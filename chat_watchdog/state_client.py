"""Read Sidecar's authoritative ConversationState without owning browser lifecycle."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .contracts import parse_conversation_state

DEFAULT_STATE_URL = "http://127.0.0.1:7337/internal/conversation-state"
_EXACT_CONVERSATION = re.compile(
    r"/c/([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})/?$", re.I
)


class StateUnavailable(RuntimeError):
    """The authoritative owner cannot currently provide exact state."""


class StateProtocolError(RuntimeError):
    """The owner response violates the shared conversation-runtime contract."""


def _post(endpoint: str, payload: Mapping[str, object]):
    request = Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=5.0) as response:
        if response.status != 200:
            raise StateUnavailable(f"state owner returned HTTP {response.status}")
        return json.loads(response.read().decode("utf-8"))


def _validate_owner(endpoint: str, expected_path: str) -> str:
    url = urlsplit(endpoint)
    if (
        url.scheme != "http"
        or url.hostname not in {"127.0.0.1", "localhost", "::1"}
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path != expected_path
    ):
        raise ValueError(f"state owner must be the localhost Sidecar {expected_path} endpoint")
    return endpoint


def sibling_state_endpoint(intent_endpoint: str) -> str:
    url = urlsplit(intent_endpoint)
    if (
        url.scheme != "http"
        or url.hostname not in {"127.0.0.1", "localhost", "::1"}
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path != "/internal/conversation-intents"
    ):
        raise ValueError("intent owner must be the localhost Sidecar intent endpoint")
    return urlunsplit((url.scheme, url.netloc, "/internal/conversation-state", "", ""))


def _conversation_id(url: str) -> str | None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "chatgpt.com"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return None
    match = _EXACT_CONVERSATION.search(parsed.path)
    return match.group(1).lower() if match else None


class SidecarStateClient:
    def __init__(self, endpoint: str = DEFAULT_STATE_URL, *, request_json=_post):
        self.endpoint = _validate_owner(endpoint, "/internal/conversation-state")
        self._request = request_json

    def read(self, target: str):
        expected_id = _conversation_id(target)
        if expected_id is None:
            raise ValueError("exact conversation target is required")
        try:
            payload = self._request(self.endpoint, {"target": target})
        except StateUnavailable:
            raise
        except StateProtocolError:
            raise
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise StateProtocolError("invalid JSON from state owner") from error
        except Exception as error:
            raise StateUnavailable(str(error)) from error

        if not isinstance(payload, Mapping) or not isinstance(payload.get("found"), bool):
            raise StateProtocolError("invalid state owner response")
        if payload["found"] is False:
            if set(payload) - {"found", "reason"}:
                raise StateProtocolError("invalid unavailable-state response")
            reason = payload.get("reason")
            if not isinstance(reason, str) or not reason:
                raise StateProtocolError("unavailable-state reason is required")
            raise StateUnavailable(reason)
        if set(payload) != {"found", "state"}:
            raise StateProtocolError("invalid found-state response")
        try:
            state = parse_conversation_state(payload["state"])
        except Exception as error:
            raise StateProtocolError(str(error)) from error
        actual_id = _conversation_id(state.to_dict()["target"])
        if actual_id is None or actual_id != expected_id:
            raise StateProtocolError("authoritative state target identity mismatch")
        return state
