from __future__ import annotations

import json
from typing import Protocol


class RelayCdpError(RuntimeError):
    pass


class WebSocketLike(Protocol):
    def send(self, payload: str) -> object: ...

    def recv(self) -> str | bytes: ...


class RelayCdpProtocol:
    def __init__(self, socket: WebSocketLike) -> None:
        self._socket = socket
        self._next_id = 1

    def _request_id(self) -> int:
        request_id = self._next_id
        self._next_id += 1
        return request_id

    def _send(self, message: dict) -> None:
        self._socket.send(json.dumps(message))

    def _recv(self) -> dict:
        try:
            raw = self._socket.recv()
        except Exception as error:
            raise RelayCdpError(f"relay transport failed: {error}") from error
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            message = json.loads(raw)
        except (TypeError, ValueError) as error:
            raise RelayCdpError(f"relay returned invalid JSON: {error}") from error
        if not isinstance(message, dict):
            raise RelayCdpError("relay returned a non-object CDP message")
        return message

    def attach_target(self, target_id: str) -> str:
        request_id = self._request_id()
        self._send(
            {
                "id": request_id,
                "method": "Target.attachToTarget",
                "params": {"targetId": target_id, "flatten": True},
            }
        )
        while True:
            message = self._recv()
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
            message = self._recv()
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise RelayCdpError(str(message["error"]))
            result = message.get("result", {}).get("result", {})
            if result.get("subtype") == "error":
                raise RelayCdpError(str(result.get("description") or result))
            return result.get("value")
