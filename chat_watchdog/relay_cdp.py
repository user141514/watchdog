from __future__ import annotations

import json
import math
import time
from typing import Protocol


class RelayCdpError(RuntimeError):
    pass


class WebSocketLike(Protocol):
    def send(self, payload: str) -> object: ...

    def recv(self) -> str | bytes: ...


class RelayCdpProtocol:
    """Bound the complete RPC, not just inactivity between incoming events."""

    def __init__(self, socket: WebSocketLike, *, request_timeout: float = 3.0,
                 clock=time.monotonic) -> None:
        if not math.isfinite(request_timeout) or request_timeout <= 0:
            raise ValueError("request timeout must be finite and positive")
        self._socket = socket
        self._next_id = 1
        self._request_timeout = request_timeout
        self._clock = clock

    def _request_id(self) -> int:
        request_id = self._next_id
        self._next_id += 1
        return request_id

    def _set_timeout(self, seconds: float) -> None:
        # Real websocket-client connections support this; deterministic test
        # transports may provide their own already-bounded receive operation.
        setter = getattr(self._socket, "settimeout", None)
        if callable(setter):
            try:
                setter(seconds)
            except Exception as error:
                raise RelayCdpError(f"relay timeout configuration failed: {error}") from error

    def _start_request(self) -> float:
        deadline = self._clock() + self._request_timeout
        # Do not leave the next send using the previous RPC's residual timeout.
        self._set_timeout(self._request_timeout)
        return deadline

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise RelayCdpError("relay response deadline exceeded")
        return remaining

    def _send(self, message: dict) -> None:
        payload = json.dumps(message)
        try:
            self._socket.send(payload)
        except Exception as error:
            raise RelayCdpError(f"relay transport failed: {error}") from error

    def _recv(self, deadline: float | None = None) -> dict:
        if deadline is None:
            deadline = self._clock() + self._request_timeout
        self._set_timeout(self._remaining(deadline))
        try:
            raw = self._socket.recv()
        except Exception as error:
            raise RelayCdpError(f"relay transport failed: {error}") from error
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            message = json.loads(raw)
        except (TypeError, ValueError) as error:
            raise RelayCdpError(f"relay returned invalid JSON: {error}") from error
        self._remaining(deadline)
        if not isinstance(message, dict):
            raise RelayCdpError("relay returned a non-object CDP message")
        return message

    def attach_target(self, target_id: str) -> str:
        request_id = self._request_id()
        deadline = self._start_request()
        self._send(
            {
                "id": request_id,
                "method": "Target.attachToTarget",
                "params": {"targetId": target_id, "flatten": True},
            }
        )
        while True:
            message = self._recv(deadline)
            if message.get("id") == request_id:
                if "error" in message:
                    raise RelayCdpError(str(message["error"]))
                session_id = message.get("result", {}).get("sessionId")
                if session_id:
                    return str(session_id)
            if message.get("method") == "Target.attachedToTarget":
                params = message.get("params") or {}
                target = params.get("targetInfo") or {}
                if target.get("targetId") == target_id and params.get("sessionId"):
                    return str(params["sessionId"])

    def evaluate(
        self,
        session_id: str,
        expression: str,
        *,
        await_promise: bool = False,
    ):
        request_id = self._request_id()
        deadline = self._start_request()
        params = {"expression": expression, "returnByValue": True}
        if await_promise:
            params["awaitPromise"] = True
        self._send(
            {
                "id": request_id,
                "sessionId": session_id,
                "method": "Runtime.evaluate",
                "params": params,
            }
        )
        while True:
            message = self._recv(deadline)
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise RelayCdpError(str(message["error"]))
            result = message.get("result", {}).get("result", {})
            if result.get("subtype") == "error":
                raise RelayCdpError(str(result.get("description") or result))
            return result.get("value")
