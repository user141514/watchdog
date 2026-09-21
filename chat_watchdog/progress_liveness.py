from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from threading import RLock
import time


@dataclass(frozen=True)
class ProgressPulse:
    progress_seq: int
    observable: bool
    last_progress_at: float | None
    last_stop_seq: int | None
    file_bytes: int


class MymemLiteStore:
    """Append-only, ephemeral liveness projection for one watched conversation."""

    def __init__(self, root: str | Path, *, clock=time.time) -> None:
        self.root = Path(root)
        self._clock = clock
        self._lock = RLock()
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, conversation_id: str) -> Path:
        if not isinstance(conversation_id, str) or not conversation_id:
            raise ValueError("conversation_id is required")
        if any(ch not in "0123456789abcdefABCDEF-" for ch in conversation_id):
            raise ValueError("conversation_id contains unsafe filename characters")
        return self.root / f"{conversation_id}.jsonl"

    def _append(self, path: Path, event: dict) -> None:
        encoded = json.dumps(
            event,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        with path.open("ab") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())

    def _read(self, conversation_id: str) -> dict:
        path = self.path_for(conversation_id)
        if not path.exists():
            raise FileNotFoundError(path)
        state = {
            "target_url": None,
            "progress_seq": 0,
            "observable": False,
            "fingerprint": None,
            "last_progress_at": None,
            "last_stop_seq": None,
            "last_stop_settled_seq": None,
        }
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            event = json.loads(line)
            kind = event.get("kind")
            if kind == "created":
                state["target_url"] = event["target_url"]
            elif kind == "progress":
                state["progress_seq"] = int(event["seq"])
                state["observable"] = True
                state["fingerprint"] = event["fingerprint"]
                state["last_progress_at"] = float(event["at"])
            elif kind == "suspend":
                state["observable"] = False
            elif kind == "stop_claim":
                state["last_stop_seq"] = int(event["seq"])
            elif kind == "stop_settled":
                state["last_stop_settled_seq"] = int(event["seq"])
        return state

    def _snapshot(self, conversation_id: str, state: dict) -> ProgressPulse:
        return ProgressPulse(
            progress_seq=int(state["progress_seq"]),
            observable=bool(state["observable"]),
            last_progress_at=state["last_progress_at"],
            last_stop_seq=state["last_stop_seq"],
            file_bytes=self.path_for(conversation_id).stat().st_size,
        )

    def ensure(self, conversation_id: str, target_url: str) -> ProgressPulse:
        with self._lock:
            path = self.path_for(conversation_id)
            if not path.exists():
                self._append(path, {
                    "schema": 1,
                    "kind": "created",
                    "at": self._clock(),
                    "conversation_id": conversation_id,
                    "target_url": target_url,
                })
            state = self._read(conversation_id)
            if state["target_url"] != target_url:
                raise ValueError("mymem_lite target identity mismatch")
            return self._snapshot(conversation_id, state)

    def suspend(self, conversation_id: str) -> ProgressPulse:
        with self._lock:
            state = self._read(conversation_id)
            if state["observable"]:
                self._append(self.path_for(conversation_id), {
                    "schema": 1,
                    "kind": "suspend",
                    "at": self._clock(),
                    "seq": state["progress_seq"],
                })
                state["observable"] = False
            return self._snapshot(conversation_id, state)

    def observe(
        self,
        conversation_id: str,
        target_url: str,
        fingerprint: str | None,
        *,
        observable: bool,
    ) -> ProgressPulse:
        with self._lock:
            self.ensure(conversation_id, target_url)
            state = self._read(conversation_id)
            if not observable:
                if state["observable"]:
                    self._append(self.path_for(conversation_id), {
                        "schema": 1,
                        "kind": "suspend",
                        "at": self._clock(),
                        "seq": state["progress_seq"],
                    })
                    state["observable"] = False
                return self._snapshot(conversation_id, state)

            if not isinstance(fingerprint, str) or not fingerprint:
                raise ValueError("observable progress requires a fingerprint")
            digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
            if not state["observable"] or state["fingerprint"] != digest:
                seq = int(state["progress_seq"]) + 1
                now = self._clock()
                self._append(self.path_for(conversation_id), {
                    "schema": 1,
                    "kind": "progress",
                    "at": now,
                    "seq": seq,
                    "fingerprint": digest,
                })
                state.update(
                    progress_seq=seq,
                    observable=True,
                    fingerprint=digest,
                    last_progress_at=now,
                )
            return self._snapshot(conversation_id, state)

    def claim_stall(
        self,
        conversation_id: str,
        *,
        timeout_seconds: float,
        state_version: int,
        writer_epoch: int,
    ) -> bool:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        with self._lock:
            state = self._read(conversation_id)
            last = state["last_progress_at"]
            if (
                not state["observable"]
                or last is None
                or self._clock() - float(last) < timeout_seconds
            ):
                return False
            if state["last_stop_seq"] == state["progress_seq"]:
                return state["last_stop_settled_seq"] != state["progress_seq"]
            self._append(self.path_for(conversation_id), {
                "schema": 1,
                "kind": "stop_claim",
                "at": self._clock(),
                "seq": state["progress_seq"],
                "state_version": int(state_version),
                "writer_epoch": int(writer_epoch),
            })
            return True

    def settle_stall(self, conversation_id: str) -> ProgressPulse:
        with self._lock:
            state = self._read(conversation_id)
            seq = int(state["progress_seq"])
            if state["last_stop_seq"] == seq and state["last_stop_settled_seq"] != seq:
                self._append(self.path_for(conversation_id), {
                    "schema": 1,
                    "kind": "stop_settled",
                    "at": self._clock(),
                    "seq": seq,
                })
                state["last_stop_settled_seq"] = seq
            return self._snapshot(conversation_id, state)

    def remove(self, conversation_id: str) -> None:
        with self._lock:
            try:
                self.path_for(conversation_id).unlink()
            except FileNotFoundError:
                pass
