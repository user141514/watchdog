from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Mapping, Protocol

from .model import PageSnapshot, Phase, PromptDelivery, TurnKey


class ReanchorBridgeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ReanchorOutcome(str, Enum):
    CONTINUED = "continued"
    WAITING = "waiting"
    DONE = "done"
    NEED_INPUT = "need_input"
    DELIVERY_UNCERTAIN = "delivery_uncertain"
    BLOCKED = "blocked"


class ReanchorCliPort(Protocol):
    def call(self, command: str, payload: Mapping[str, object]) -> dict: ...


class ReanchorPagePort(Protocol):
    target_url: str

    def send_prompt(
        self,
        prompt: str,
        expected_turn_key: TurnKey,
    ) -> PromptDelivery: ...


@dataclass
class ReanchorCli:
    store: str
    cli_path: str
    node_executable: str = "node"
    timeout_seconds: float = 10.0

    def call(self, command: str, payload: Mapping[str, object]) -> dict:
        process = subprocess.run(
            [
                self.node_executable,
                str(Path(self.cli_path)),
                command,
                "--store",
                self.store,
            ],
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        if process.returncode != 0:
            code = "REANCHOR_CLI_ERROR"
            message = process.stderr.strip() or f"reanchor CLI exited {process.returncode}"
            try:
                body = json.loads(process.stderr)
                error = body.get("error") or {}
                code = str(error.get("code") or code)
                message = str(error.get("message") or message)
            except (TypeError, ValueError):
                pass
            raise ReanchorBridgeError(code, message)
        try:
            result = json.loads(process.stdout)
        except ValueError as error:
            raise ReanchorBridgeError(
                "INVALID_REANCHOR_OUTPUT",
                "reanchor CLI returned invalid JSON",
            ) from error
        if not isinstance(result, dict):
            raise ReanchorBridgeError(
                "INVALID_REANCHOR_OUTPUT",
                "reanchor CLI returned a non-object result",
            )
        return result


@dataclass
class ReanchorBridge:
    cli: ReanchorCliPort
    scope: str
    owner: str
    context: Mapping[str, object]

    def _identity(self) -> dict[str, str]:
        return {"scope": self.scope, "owner": self.owner}

    def _status(self) -> dict:
        return self.cli.call("status", {"scope": self.scope})

    @staticmethod
    def _assistant_source_id(snapshot: PageSnapshot) -> str:
        digest = hashlib.sha256(snapshot.assistant_text.encode("utf-8")).hexdigest()[:16]
        return f"chatgpt-assistant:{snapshot.assistant_turn_id}:{digest}"

    def _sync_sources(
        self,
        snapshot: PageSnapshot,
        *,
        target: str,
        delivery_message_ids: set[str],
    ) -> None:
        identity = self._identity()
        self.cli.call("bind", {**identity, "context": dict(self.context)})
        if (
            snapshot.user_turn_id
            and snapshot.user_text.strip()
            and snapshot.user_turn_id not in delivery_message_ids
        ):
            digest = hashlib.sha256(snapshot.user_text.encode("utf-8")).hexdigest()[:16]
            self.cli.call(
                "observe",
                {
                    **identity,
                    "source": {
                        "id": f"chatgpt-dom-user:{snapshot.user_turn_id}:{digest}",
                        "role": "runtime",
                        "text": snapshot.user_text,
                        "origin": {
                            "kind": "chatgpt-dom",
                            "target": target,
                            "turnId": snapshot.user_turn_id,
                            "observedRole": "user",
                            "authority": False,
                        },
                    },
                },
            )
        if snapshot.assistant_turn_id and snapshot.assistant_text.strip():
            self.cli.call(
                "observe",
                {
                    **identity,
                    "source": {
                        "id": self._assistant_source_id(snapshot),
                        "role": "assistant",
                        "text": snapshot.assistant_text,
                        "origin": {
                            "kind": "chatgpt-dom",
                            "target": target,
                            "turnId": snapshot.assistant_turn_id,
                        },
                    },
                },
            )

    def record_frontend_fault(
        self,
        page: ReanchorPagePort,
        snapshot: PageSnapshot,
    ) -> None:
        target = page.target_url
        state = self._status()
        if not state.get("scope"):
            raise ReanchorBridgeError(
                "REANCHOR_SCOPE_NOT_FOUND",
                f"reanchor scope {self.scope!r} is not initialized",
            )
        if state.get("target") != target:
            raise ReanchorBridgeError(
                "TARGET_MISMATCH",
                "reanchor scope target does not match the watched conversation",
            )
        delivery_message_ids = {
            str(message_id)
            for message_id in (state.get("deliveryMessageIds") or [])
            if message_id
        }
        self._sync_sources(
            snapshot,
            target=target,
            delivery_message_ids=delivery_message_ids,
        )
        if snapshot.send_timeout:
            fault = "send_timeout"
        elif snapshot.stream_interrupted:
            fault = "stream_interrupted"
        else:
            return
        text = snapshot.fault_text.strip() or f"frontend fault observed: {fault}"
        text = text[:4096]
        digest = hashlib.sha256(
            f"{fault}\0{text}\0{snapshot.user_turn_id}\0{snapshot.assistant_turn_id}".encode("utf-8")
        ).hexdigest()[:16]
        self.cli.call(
            "observe",
            {
                **self._identity(),
                "source": {
                    "id": f"chatgpt-frontend-fault:{fault}:{digest}",
                    "role": "runtime",
                    "text": text,
                    "origin": {
                        "kind": "chatgpt-frontend-fault",
                        "fault": fault,
                        "target": target,
                        "userTurnId": snapshot.user_turn_id,
                        "assistantTurnId": snapshot.assistant_turn_id,
                    },
                },
            },
        )

    def _reconcile_sending_delivery(
        self,
        snapshot: PageSnapshot,
        *,
        target: str,
        pending: Mapping[str, object],
    ) -> bool:
        packet_id = str(pending.get("packetId") or "")
        if (
            not packet_id
            or not snapshot.user_turn_id
            or not snapshot.user_text.strip()
            or packet_id not in snapshot.user_text
            or "REANCHOR_RESULT" not in snapshot.user_text
        ):
            return False
        self.cli.call(
            "delivered",
            {
                **self._identity(),
                "packetId": packet_id,
                "target": target,
                "messageId": snapshot.user_turn_id,
            },
        )
        return True

    def _accept_pending_reply(
        self,
        snapshot: PageSnapshot,
        *,
        target: str,
        pending: Mapping[str, object],
    ) -> dict | None:
        packet_id = str(pending.get("packetId") or "")
        message_id = str(pending.get("messageId") or "")
        if not packet_id or not message_id:
            return None
        if snapshot.user_turn_id != message_id:
            return None
        if snapshot.user_turn_pending or snapshot.phase is not Phase.FINISHED:
            return None
        if not snapshot.assistant_turn_id or not snapshot.assistant_text.strip():
            return None
        return self.cli.call(
            "accept",
            {
                **self._identity(),
                "packetId": packet_id,
                "response": {
                    "role": "assistant",
                    "final": True,
                    "target": target,
                    "replyTo": message_id,
                    "messageId": snapshot.assistant_turn_id,
                    "text": snapshot.assistant_text,
                },
            },
        )

    def step(
        self,
        page: ReanchorPagePort,
        snapshot: PageSnapshot,
        *,
        reason: str = "resume",
    ) -> ReanchorOutcome:
        target = page.target_url
        state = self._status()
        if not state.get("scope"):
            raise ReanchorBridgeError(
                "REANCHOR_SCOPE_NOT_FOUND",
                f"reanchor scope {self.scope!r} is not initialized",
            )
        if state.get("target") != target:
            raise ReanchorBridgeError(
                "TARGET_MISMATCH",
                "reanchor scope target does not match the watched conversation",
            )

        pending = state.get("pending")
        if isinstance(pending, Mapping) and pending.get("stage") == "SENDING":
            if not self._reconcile_sending_delivery(
                snapshot,
                target=target,
                pending=pending,
            ):
                return ReanchorOutcome.DELIVERY_UNCERTAIN
            state = self._status()
            pending = state.get("pending")

        delivery_message_ids = {
            str(message_id)
            for message_id in (state.get("deliveryMessageIds") or [])
            if message_id
        }
        self._sync_sources(
            snapshot,
            target=target,
            delivery_message_ids=delivery_message_ids,
        )
        state = self._status()
        pending = state.get("pending")
        authority_changed = bool(state.get("authorityChangedSinceCheckpoint"))

        if isinstance(pending, Mapping) and pending.get("stage") == "DELIVERED":
            accepted = self._accept_pending_reply(
                snapshot,
                target=target,
                pending=pending,
            )
            if accepted is None:
                return ReanchorOutcome.WAITING
            if accepted.get("status") == "REJECTED":
                return ReanchorOutcome.BLOCKED
            if accepted.get("status") == "CLAIM_RECORDED":
                reported = accepted.get("reportedState")
                if reported == "COMPLETE":
                    return ReanchorOutcome.DONE
                if reported == "NEED_INPUT":
                    return ReanchorOutcome.NEED_INPUT
                if reported == "NEED_CONTEXT":
                    return ReanchorOutcome.BLOCKED
            state = self._status()
            pending = state.get("pending")

        if state.get("lastRejection") and not authority_changed:
            return ReanchorOutcome.BLOCKED

        checkpoint = state.get("checkpoint")
        if isinstance(checkpoint, Mapping) and not authority_changed:
            checkpoint_state = checkpoint.get("state")
            if checkpoint_state == "COMPLETE":
                return ReanchorOutcome.DONE
            if checkpoint_state == "NEED_INPUT":
                return ReanchorOutcome.NEED_INPUT
            if checkpoint_state == "NEED_CONTEXT":
                return ReanchorOutcome.BLOCKED

        prepared = self.cli.call(
            "prepare",
            {
                **self._identity(),
                "reason": reason,
                "safe": True,
            },
        )
        if prepared.get("status") == "DEFERRED":
            return ReanchorOutcome.BLOCKED
        packet_id = str(prepared.get("id") or "")
        prompt = prepared.get("prompt")
        if not packet_id or not isinstance(prompt, str) or not prompt:
            raise ReanchorBridgeError(
                "INVALID_REANCHOR_PACKET",
                "prepare did not return a packet id and prompt",
            )

        self.cli.call(
            "claim",
            {**self._identity(), "packetId": packet_id},
        )
        delivery = page.send_prompt(prompt, snapshot.turn_key)
        if not delivery.accepted or not delivery.message_id:
            return ReanchorOutcome.DELIVERY_UNCERTAIN
        self.cli.call(
            "delivered",
            {
                **self._identity(),
                "packetId": packet_id,
                "target": target,
                "messageId": delivery.message_id,
            },
        )
        return ReanchorOutcome.CONTINUED
