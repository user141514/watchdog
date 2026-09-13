from __future__ import annotations

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from threading import RLock
from typing import Callable, Protocol
from urllib.parse import urlsplit
from uuid import UUID


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


@dataclass(frozen=True)
class WatchCompletion:
    conversation_id: str
    target_url: str
    result: str | None


@dataclass
class _WatchEntry:
    conversation_id: str
    target_url: str
    watcher: Watcher


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
    """Thread-safe in-memory map from ChatGPT conversation UUID to watcher."""

    def __init__(self, watcher_factory: Callable[[str], Watcher]) -> None:
        self._watcher_factory = watcher_factory
        self._watchers: dict[str, _WatchEntry] = {}
        self._completed: dict[str, WatchCompletion] = {}
        self._lock = RLock()

    def register(self, target_url: str) -> RegisterResult:
        conversation_id = conversation_id_from_url(target_url)
        with self._lock:
            if conversation_id in self._watchers:
                return RegisterResult(conversation_id=conversation_id, created=False)
            self._completed.pop(conversation_id, None)
            watcher = self._watcher_factory(target_url)
            self._watchers[conversation_id] = _WatchEntry(
                conversation_id=conversation_id,
                target_url=target_url,
                watcher=watcher,
            )
        return RegisterResult(conversation_id=conversation_id, created=True)

    def unregister(self, conversation: str) -> bool:
        conversation_id = _conversation_id(conversation)
        with self._lock:
            entry = self._watchers.pop(conversation_id, None)
        if entry is None:
            return False
        entry.watcher.close()
        return True

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
            return self._completed.pop(conversation_id, None) is not None

    def list(self) -> list[WatchRegistration]:
        with self._lock:
            return [
                WatchRegistration(
                    entry.conversation_id,
                    entry.target_url,
                    getattr(entry.watcher, "state", "active"),
                )
                for entry in sorted(
                    self._watchers.values(),
                    key=lambda item: item.conversation_id,
                )
            ]

    def step_all(self) -> None:
        with self._lock:
            snapshot = list(self._watchers.items())

        completed: list[tuple[str, Watcher]] = []
        for conversation_id, entry in snapshot:
            entry.watcher.step()
            if entry.watcher.should_stop:
                completed.append((conversation_id, entry.watcher))

        for conversation_id, watcher in completed:
            with self._lock:
                current = self._watchers.get(conversation_id)
                if current is None or current.watcher is not watcher:
                    continue
                self._completed[conversation_id] = WatchCompletion(
                    conversation_id=conversation_id,
                    target_url=current.target_url,
                    result=watcher.completion_text,
                )
                self._watchers.pop(conversation_id)
            watcher.close()

    def close(self) -> None:
        with self._lock:
            entries = list(self._watchers.values())
            self._watchers.clear()
            self._completed.clear()
        for entry in entries:
            entry.watcher.close()


def create_control_server(
    registry: WatchRegistry,
    host: str = "127.0.0.1",
    port: int = 9235,
) -> HTTPServer:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("watchdog control server must bind to localhost")

    class ControlHandler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return None

        def _send_json(self, status: int, payload: object) -> None:
            encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

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
            if self.path != "/watches":
                self._send_json(404, {"error": "not found"})
                return
            self._send_json(
                200,
                {
                    "watches": [
                        {
                            "conversation_id": entry.conversation_id,
                            "target_url": entry.target_url,
                            "state": entry.state,
                        }
                        for entry in registry.list()
                    ]
                },
            )

        def do_POST(self) -> None:
            try:
                payload = self._read_json()
                if self.path == "/register":
                    target_url = payload.get("url")
                    if not isinstance(target_url, str):
                        raise ValueError("url must be a string")
                    result = registry.register(target_url)
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
                    removed = registry.unregister(conversation_id)
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
            except RuntimeError as error:
                self._send_json(409, {"error": str(error)})

    return HTTPServer((host, port), ControlHandler)
