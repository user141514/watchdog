from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Callable, Mapping
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class SendAdmissionResult:
    admitted: bool
    retry_after_ms: int | None = None


def _post_json(endpoint: str, payload: Mapping[str, object]) -> object:
    request = Request(
        endpoint,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=0.5) as response:
        if response.status != 200:
            raise RuntimeError(f"send admission returned HTTP {response.status}")
        return json.loads(response.read().decode("utf-8"))


class SidecarSendAdmission:
    def __init__(
        self,
        endpoint: str,
        *,
        request_json: Callable[[str, Mapping[str, object]], object] = _post_json,
    ) -> None:
        if not isinstance(endpoint, str) or not endpoint:
            raise ValueError("send admission endpoint must be a non-empty string")
        self.endpoint = endpoint
        self._request_json = request_json

    def admit(self, target_url: str) -> SendAdmissionResult:
        payload = self._request_json(
            self.endpoint,
            {"source": "watchdog", "target": target_url},
        )
        if not isinstance(payload, Mapping) or not isinstance(payload.get("admitted"), bool):
            raise RuntimeError("invalid send admission response")
        retry = payload.get("retryAfterMs")
        return SendAdmissionResult(
            admitted=payload["admitted"],
            retry_after_ms=retry if isinstance(retry, int) else None,
        )
