from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, RLock
from typing import Protocol
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

from .registry_store import RegistryStore

_LOG = logging.getLogger(__name__)


class RegistrationRejected(RuntimeError):
    def __init__(self, code: str, reason: str) -> None:
        self.code = code
        self.reason = reason
        super().__init__(f"{code}: {reason}")


class WithdrawalUnconfirmed(RuntimeError):
    """Desired membership is removed, but remote effect quiescence is unconfirmed."""


class Watcher(Protocol):
    should_stop: bool

    @property
    def completion_text(self) -> str | None: ...

    @property
    def state(self) -> str: ...

    def step(self) -> object: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class RegisterResult:
    conversation_id: str
    created: bool


@dataclass(frozen=True)
class WatchRegistration:
    conversation_id: str
    target_url: str
    state: str = "active"
    connected: bool = False
    registered_at: float | None = None
    last_poll_at: float | None = None
    last_success_at: float | None = None
    consecutive_failures: int = 0
    last_error: str | None = None
    diagnostics: dict | None = None
    last_registration: dict | None = None
    registration_id: str | None = None


@dataclass(frozen=True)
class WatchCompletion:
    conversation_id: str
    target_url: str
    result: str | None


@dataclass
class _WatchEntry:
    conversation_id: str
    target_url: str
    watcher: Watcher | None = None
    registered_at: float | None = None
    last_poll_at: float | None = None
    last_success_at: float | None = None
    consecutive_failures: int = 0
    last_error: str | None = None
    runtime_state: dict[str, object] | None = None
    last_registration: dict | None = None
    registration_id: str = field(default_factory=lambda: str(uuid4()))
    lock: object = field(default_factory=RLock)


def conversation_id_from_url(value: str) -> str:
    """Return ChatGPT's own stable conversation UUID from a conversation URL."""
    if not isinstance(value, str) or not value:
        raise ValueError("conversation URL must be a non-empty string")

    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname != "chatgpt.com":
        raise ValueError("conversation URL must use https://chatgpt.com")

    segments = [segment for segment in parsed.path.split("/") if segment]
    try:
        c_index = len(segments) - 2 - segments[-2::-1].index("c")
    except ValueError as error:
        raise ValueError("conversation URL must contain /c/<conversation-id>") from error

    if c_index + 1 >= len(segments):
        raise ValueError("conversation URL must contain /c/<conversation-id>")

    candidate = segments[c_index + 1]
    try:
        conversation_id = str(UUID(candidate))
    except (ValueError, AttributeError) as error:
        raise ValueError("conversation id must be a UUID") from error

    if c_index + 2 != len(segments):
        raise ValueError("conversation URL must end with /c/<conversation-id>")
    return conversation_id


def _conversation_id(value: str) -> str:
    if "://" in value:
        return conversation_id_from_url(value)
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as error:
        raise ValueError("conversation id must be a UUID") from error


class WatchRegistry:
    """Durable desired watches with disposable, independently recoverable bindings.

    The registry lock protects identity and storage only, never network I/O.
    Per-conversation locks fence stale poll snapshots and serialize withdrawal
    with in-flight work. The Sidecar remains the sole managed browser writer.
    """

    def __init__(self, watcher_factory: Callable[[str], Watcher], *,
                 store_path: str | Path | None = None, clock=time.time,
                 connect_on_register: bool = True,
                 registration_preflight: Callable[[str], None] | None = None,
                 withdrawal_callback: Callable[[str, str], dict] | None = None,
                 progress_store=None) -> None:
        self._watcher_factory = watcher_factory
        self._connect_on_register = connect_on_register
        self._registration_preflight = registration_preflight
        self._withdrawal_callback = withdrawal_callback
        self._pending_withdrawals: dict[str, dict] = {}
        self._withdrawn_entries: dict[str, _WatchEntry] = {}
        self._progress_store = progress_store
        self._clock = clock
        self._watchers: dict[str, _WatchEntry] = {}
        self._completed: dict[str, WatchCompletion] = {}
        self._lock = RLock()
        self._poll_lock = Lock()
        self._entry_locks: dict[str, object] = {}
        self._closed = False
        self._last_poll_started_at: float | None = None
        self._last_poll_completed_at: float | None = None
        self._last_poll_error: str | None = None
        self.instance_id = str(uuid4())
        self._store = RegistryStore(store_path)
        try:
            self._pending_withdrawals = {
                row["conversation_id"]: row for row in self._store.load_withdrawals()
            }
            for row in self._store.load():
                conversation_id = conversation_id_from_url(row["target_url"])
                if conversation_id != row["conversation_id"]:
                    raise RuntimeError("stored conversation identity mismatch")
                if row["status"] == "completed":
                    self._completed[conversation_id] = WatchCompletion(
                        conversation_id, row["target_url"], row["result"])
                else:
                    lock = self._entry_locks.setdefault(conversation_id, RLock())
                    self._watchers[conversation_id] = _WatchEntry(
                        conversation_id, row["target_url"], lock=lock,
                        registered_at=row["registered_at"],
                        last_poll_at=row["last_poll_at"],
                        last_success_at=row["last_success_at"],
                        consecutive_failures=row["consecutive_failures"],
                        last_error=row["last_error"],
                        runtime_state=row.get("runtime_state"),
                        last_registration=row.get("last_registration"),
                        registration_id=row["registration_id"],
                    )
                    if self._progress_store is not None:
                        self._progress_store.ensure(conversation_id, row["target_url"])
                        # A daemon restart breaks the continuous observation
                        # window. Never carry an old stall deadline across it.
                        self._progress_store.suspend(conversation_id)
        except BaseException:
            self._store.close()
            raise

    def _current(self, entry: _WatchEntry) -> bool:
        return not self._closed and self._watchers.get(entry.conversation_id) is entry

    def _record_error(self, entry: _WatchEntry, error: Exception) -> None:
        with self._lock:
            if not self._current(entry):
                return
            entry.consecutive_failures += 1
            entry.last_error = f"{type(error).__name__}: {error}"[:1000]
            self._store.observe(entry)
        _LOG.warning("watchdog %s retained for retry: %s", entry.conversation_id, entry.last_error)

    def _bind(self, entry: _WatchEntry) -> bool:
        try:
            watcher = self._watcher_factory(entry.target_url)
            bind_registration = getattr(watcher, "bind_registration", None)
            if callable(bind_registration):
                bind_registration(entry.registration_id)
            restore_state = getattr(watcher, "restore_state", None)
            if entry.runtime_state is not None and callable(restore_state):
                restore_state(deepcopy(entry.runtime_state))
            persist = getattr(watcher, "set_persistence_callback", None)
            if callable(persist):
                persist(lambda state: self._persist_runtime_state(entry, state))
            entry.watcher = watcher
            return True
        except Exception as error:  # noqa: BLE001 - isolate arbitrary transport plugins
            self._record_error(entry, error)
            return False

    def _persist_runtime_state(self, entry: _WatchEntry, state: dict) -> None:
        with self._lock:
            if not self._current(entry):
                raise RuntimeError("watch generation was withdrawn before effect reservation")
            self._store.reserve_runtime_state(entry.conversation_id, entry.registration_id, state)
            entry.runtime_state = deepcopy(state)

    @staticmethod
    def _close_watcher(entry: _WatchEntry) -> None:
        WatchRegistry._close_transport(entry.conversation_id, entry.watcher)

    @staticmethod
    def _close_transport(conversation_id: str, watcher: Watcher | None) -> None:
        if watcher is not None:
            try:
                watcher.close()
            except Exception:
                _LOG.exception("watchdog transport cleanup failed: %s", conversation_id)

    def _provenance(self, metadata: dict | None, reason: str) -> dict:
        metadata = metadata or {}
        return {
            "source": metadata.get("source", "python-api"),
            "actor": metadata.get("actor", "local-controller"),
            "operation_id": metadata.get("operation_id", str(uuid4())),
            "reason": metadata.get("reason", reason),
            "at": self._clock(),
        }

    def register(self, target_url: str, *, provenance: dict | None = None) -> RegisterResult:
        conversation_id = conversation_id_from_url(target_url)
        with self._lock:
            if self._closed:
                raise RuntimeError("watch registry is closed")
        if self._registration_preflight is not None:
            self._registration_preflight(target_url)
        with self._lock:
            if self._closed:
                raise RuntimeError("watch registry is closed")
            if conversation_id in self._pending_withdrawals:
                raise RegistrationRejected("withdrawal_pending", "confirm prior withdrawal before rebinding")
            if conversation_id in self._watchers:
                return RegisterResult(conversation_id=conversation_id, created=False)
            metadata = self._provenance(provenance, "explicit Python registration")
            at = metadata["at"]
            if self._progress_store is not None:
                self._progress_store.ensure(conversation_id, target_url)
            try:
                registration_id = str(uuid4())
                self._store.register(conversation_id, target_url, at, metadata, registration_id)
            except BaseException:
                if self._progress_store is not None:
                    self._progress_store.remove(conversation_id)
                raise
            self._completed.pop(conversation_id, None)
            lock = self._entry_locks.setdefault(conversation_id, RLock())
            entry = _WatchEntry(conversation_id, target_url, registered_at=at, lock=lock,
                                last_registration=metadata, registration_id=registration_id)
            self._watchers[conversation_id] = entry
        if self._connect_on_register:
            with entry.lock:
                with self._lock:
                    current = self._current(entry)
                if current and entry.watcher is None:
                    self._bind(entry)
        return RegisterResult(conversation_id=conversation_id, created=True)

    def unregister(self, conversation: str, *, provenance: dict | None = None) -> bool:
        conversation_id = _conversation_id(conversation)
        with self._lock:
            entry = self._watchers.get(conversation_id)
            withdrawal = self._pending_withdrawals.get(conversation_id)
            if entry is None and withdrawal is None:
                return False
            if entry is not None:
                withdrawal = {
                    "conversation_id": conversation_id,
                    "target_url": entry.target_url,
                    "registration_id": entry.registration_id,
                    "provenance": self._provenance(provenance, "explicit unregistration"),
                }
                # Fail closed locally before remote work. This workflow survives
                # a lost response/restart and never restores desired membership.
                self._store.begin_withdrawal(withdrawal)
                self._pending_withdrawals[conversation_id] = withdrawal
                self._withdrawn_entries[conversation_id] = entry
                self._watchers.pop(conversation_id)
        self._finish_withdrawal(withdrawal)
        return True

    def _finish_withdrawal(self, withdrawal: dict) -> None:
        conversation_id = withdrawal["conversation_id"]
        with self._lock:
            lock = self._entry_locks.setdefault(conversation_id, RLock())
        with lock:
            with self._lock:
                if self._pending_withdrawals.get(conversation_id) is not withdrawal:
                    return
                entry = self._withdrawn_entries.pop(conversation_id, None)
            if entry is not None:
                self._close_watcher(entry)
            if self._progress_store is not None:
                try:
                    # Rebinding cannot race cleanup while withdrawal is pending.
                    self._progress_store.remove(conversation_id)
                except Exception:
                    _LOG.exception("mymem_lite cleanup failed: %s", conversation_id)
            if self._withdrawal_callback is not None:
                try:
                    result = self._withdrawal_callback(
                        withdrawal["registration_id"], withdrawal["target_url"],
                    )
                    if (not isinstance(result, dict) or result.get("accepted") is not True
                            or result.get("quiescent") is not True
                            or result.get("registrationId") != withdrawal["registration_id"]):
                        raise RuntimeError("owner did not confirm generation quiescence")
                except Exception as error:
                    raise WithdrawalUnconfirmed(f"withdrawal_unconfirmed: {error}") from error
            with self._lock:
                self._store.finish_withdrawal(withdrawal, self._clock())
                self._pending_withdrawals.pop(conversation_id, None)

    def has_scheduler_work(self) -> bool:
        with self._lock:
            return bool(self._watchers or self._pending_withdrawals)

    def list_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._watchers)

    def is_active(self, conversation: str) -> bool:
        conversation_id = _conversation_id(conversation)
        with self._lock:
            return conversation_id in self._watchers

    def completion(self, conversation: str) -> WatchCompletion | None:
        conversation_id = _conversation_id(conversation)
        with self._lock:
            return self._completed.get(conversation_id)

    def ack_completion(self, conversation: str) -> bool:
        conversation_id = _conversation_id(conversation)
        with self._lock:
            if conversation_id not in self._completed:
                return False
            self._store.remove(conversation_id, status="completed",
                               provenance=self._provenance(None, "completion acknowledged"))
            self._completed.pop(conversation_id)
            return True

    def list(self) -> list[WatchRegistration]:
        with self._lock:
            return [
                WatchRegistration(
                    conversation_id=entry.conversation_id,
                    target_url=entry.target_url,
                    state=("reconnecting" if entry.watcher is None else
                           "degraded" if entry.last_error else
                           getattr(entry.watcher, "state", "active")),
                    connected=entry.watcher is not None,
                    registered_at=entry.registered_at,
                    last_poll_at=entry.last_poll_at,
                    last_success_at=entry.last_success_at,
                    consecutive_failures=entry.consecutive_failures,
                    last_error=entry.last_error,
                    diagnostics=getattr(entry.watcher, "diagnostics", None),
                    last_registration=None if entry.last_registration is None else dict(entry.last_registration),
                    registration_id=entry.registration_id,
                )
                for entry in sorted(self._watchers.values(), key=lambda item: item.conversation_id)
            ]

    def health(self, *, stale_after: float = 120.0) -> dict:
        with self._lock:
            last = self._last_poll_completed_at
            fresh = (self._last_poll_error is None and (
                not self._watchers or (
                    last is not None and 0 <= self._clock() - last <= stale_after
                )
            ))
            return {
                "ready": not self._closed and fresh and not self._pending_withdrawals,
                "pending_withdrawal_count": len(self._pending_withdrawals),
                "quiescent": not self._watchers and not self._pending_withdrawals,
                "scheduler_state": ("withdrawing" if self._pending_withdrawals else
                                    "watching" if self._watchers else "empty"),
                "instance_id": self.instance_id,
                "pid": os.getpid(),
                "module_path": str(Path(__file__).resolve()),
                "protocol_version": 2,
                "last_poll_error": self._last_poll_error,
                "durable": self._store.path is not None,
                "store_path": self._store.path,
                "polling_fresh": fresh,
                "last_poll_started_at": self._last_poll_started_at,
                "last_poll_completed_at": last,
                "active_count": len(self._watchers),
                "degraded_count": sum(e.watcher is None or e.last_error is not None
                                      for e in self._watchers.values()),
            }

    def lifecycle(self, *, limit: int = 50) -> list[dict]:
        if not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("lifecycle limit must be 1..200")
        with self._lock:
            return self._store.lifecycle(limit=limit)

    def _step_entry(self, entry: _WatchEntry) -> None:
        with entry.lock:
            with self._lock:
                if not self._current(entry):
                    return
                generation = entry.registration_id
                entry.last_poll_at = self._clock()
                # A broken durable store closes the side-effect gate, not just logging.
                self._store.observe(entry)
            if entry.watcher is None and not self._bind(entry):
                return
            try:
                entry.watcher.step()
                durable_state = getattr(entry.watcher, "durable_state", None)
                if durable_state is not None:
                    if not isinstance(durable_state, dict):
                        raise TypeError("watcher durable_state must be a dict")
                    entry.runtime_state = deepcopy(durable_state)
                completed = entry.watcher.should_stop
                result = entry.watcher.completion_text if completed else None
                owner_rebind = getattr(entry.watcher, "requires_owner_rebind", False) is True
            except Exception as error:  # noqa: BLE001 - one watcher must not kill its siblings
                self._record_error(entry, error)
                return
            cached = None
            with self._lock:
                if (not self._current(entry) or entry.registration_id != generation
                        or entry.conversation_id in self._pending_withdrawals):
                    return
                entry.last_success_at = self._clock()
                entry.consecutive_failures = 0
                entry.last_error = None
                # Preserve phase, reservations and accepted-review lineage before
                # discarding a cache. Desired membership/generation remains owned
                # here; an observation or owner denial cannot register a watch.
                self._store.observe(entry)
                if owner_rebind and not completed:
                    cached = entry.watcher
                    entry.watcher = None
                if completed:
                    self._store.complete(entry.conversation_id, result,
                                         self._provenance({"source": "watchdog-runtime",
                                                           "actor": self.instance_id},
                                                          "watcher completed"))
                    self._completed[entry.conversation_id] = WatchCompletion(
                        entry.conversation_id, entry.target_url, result)
                    self._watchers.pop(entry.conversation_id)
            if cached is not None:
                self._close_transport(entry.conversation_id, cached)
            if completed:
                self._close_watcher(entry)
                if self._progress_store is not None:
                    try:
                        self._progress_store.remove(entry.conversation_id)
                    except Exception:
                        _LOG.exception("mymem_lite completion cleanup failed: %s", entry.conversation_id)

    def step_all(self) -> None:
        with self._poll_lock:
            with self._lock:
                if self._closed:
                    return
                self._last_poll_started_at = self._clock()
                snapshot = list(self._watchers.values())
                pending = list(self._pending_withdrawals.values())
            try:
                for withdrawal in pending:
                    try:
                        self._finish_withdrawal(withdrawal)
                    except WithdrawalUnconfirmed:
                        _LOG.warning("withdrawal remains unconfirmed: %s",
                                     withdrawal["conversation_id"])
                for entry in snapshot:
                    self._step_entry(entry)
            except Exception as error:
                with self._lock:
                    self._last_poll_error = f"{type(error).__name__}: {error}"[:1000]
                raise
            with self._lock:
                self._last_poll_error = None
                self._last_poll_completed_at = self._clock()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            entries = list(self._watchers.values())
            self._watchers.clear()
            self._completed.clear()
        for entry in entries:
            with entry.lock:
                self._close_watcher(entry)
        with self._lock:
            self._store.close()


def create_control_server(
    registry: WatchRegistry,
    host: str = "127.0.0.1",
    port: int = 9235,
    *,
    stale_after: float = 120.0,
    wake: Callable[[], None] | None = None,
) -> HTTPServer:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("watchdog control server must bind to localhost")

    class ControlHandler(BaseHTTPRequestHandler):
        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(5.0)

        def log_message(self, _format: str, *_args: object) -> None:
            return None

        def _send_json(self, status: int, payload: object) -> None:
            encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(encoded)))
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except (ConnectionError, TimeoutError):
                # A lost ACK is not permission to undo its already-committed effect.
                self.close_connection = True

        def _read_json(self) -> dict[str, object]:
            try:
                length = int(self.headers.get("content-length", "0"))
            except ValueError as error:
                raise ValueError("invalid content-length") from error
            if length <= 0 or length > 65536:
                raise ValueError("request body must be 1..65536 bytes")
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError("request body must be JSON") from error
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            return payload

        def do_GET(self) -> None:
            if self.path == "/health":
                self._send_json(200, registry.health(stale_after=stale_after))
                return
            parsed = urlsplit(self.path)
            if parsed.path == "/lifecycle":
                try:
                    limit = int(parse_qs(parsed.query).get("limit", ["50"])[0])
                    self._send_json(200, {"events": registry.lifecycle(limit=limit)})
                except (ValueError, TypeError) as error:
                    self._send_json(400, {"error": str(error)})
                return
            if self.path != "/watches":
                self._send_json(404, {"error": "not found"})
                return
            self._send_json(
                200,
                {
                    "watches": [asdict(entry) for entry in registry.list()]
                },
            )

        def do_POST(self) -> None:
            try:
                payload = self._read_json()
                if self.path == "/register":
                    target_url = payload.get("url")
                    if not isinstance(target_url, str):
                        raise ValueError("url must be a string")
                    # Metadata describes a trusted localhost controller action; it
                    # is provenance, not authentication or proof of human intent.
                    if (payload.get("explicit") is not True
                            or not isinstance(payload.get("source"), str)
                            or payload.get("source") not in {
                                "observatory-ui", "cli-explicit", "controlled-api"
                            }
                            or any(not isinstance(payload.get(key), str)
                                   or not payload[key].strip()
                                   or len(payload[key]) > 1000
                                   for key in ("actor", "operation_id", "reason"))):
                        raise RegistrationRejected(
                            "explicit_bind_required",
                            "register requires an explicit bind from a supported controller",
                        )
                    provenance = {key: payload[key] for key in
                                  ("source", "actor", "operation_id", "reason")}
                    result = registry.register(target_url, provenance=provenance)
                    if wake is not None:
                        wake()
                    self._send_json(
                        200,
                        {
                            "conversation_id": result.conversation_id,
                            "created": result.created,
                        },
                    )
                    return

                if self.path == "/unregister":
                    conversation = payload.get("conversation_id", payload.get("url"))
                    if not isinstance(conversation, str):
                        raise ValueError("conversation_id or url must be a string")
                    conversation_id = _conversation_id(conversation)
                    provenance = {
                        "source": payload.get("source", "controlled-api"),
                        "actor": payload.get("actor", "local-controller"),
                        "operation_id": payload.get("operation_id", str(uuid4())),
                        "reason": payload.get("reason", "explicit unregistration"),
                    }
                    if any(not isinstance(value, str) or not value.strip() or len(value) > 1000
                           for value in provenance.values()):
                        raise ValueError("unregister provenance must contain non-empty strings")
                    try:
                        removed = registry.unregister(conversation_id, provenance=provenance)
                    finally:
                        if wake is not None:
                            wake()
                    self._send_json(
                        200,
                        {"conversation_id": conversation_id, "removed": removed},
                    )
                    return

                if self.path == "/completion":
                    conversation = payload.get("conversation_id", payload.get("url"))
                    if not isinstance(conversation, str):
                        raise ValueError("conversation_id or url must be a string")
                    conversation_id = _conversation_id(conversation)
                    completion = registry.completion(conversation_id)
                    self._send_json(
                        200,
                        {
                            "conversation_id": conversation_id,
                            "active": registry.is_active(conversation_id),
                            "completed": completion is not None,
                            "result": None if completion is None else completion.result,
                        },
                    )
                    return

                if self.path == "/completion/ack":
                    conversation = payload.get("conversation_id", payload.get("url"))
                    if not isinstance(conversation, str):
                        raise ValueError("conversation_id or url must be a string")
                    conversation_id = _conversation_id(conversation)
                    removed = registry.ack_completion(conversation_id)
                    self._send_json(
                        200,
                        {"conversation_id": conversation_id, "removed": removed},
                    )
                    return

                self._send_json(404, {"error": "not found"})
            except ValueError as error:
                self._send_json(400, {"error": str(error)})
            except WithdrawalUnconfirmed as error:
                self._send_json(503, {"error": "withdrawal_unconfirmed", "reason": str(error)})
            except RegistrationRejected as error:
                self._send_json(409, {"error": error.code, "reason": error.reason})
            except RuntimeError as error:
                self._send_json(409, {"error": str(error)})

    return ThreadingHTTPServer((host, port), ControlHandler)
