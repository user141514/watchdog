from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping
from urllib.parse import urlsplit
import re

CONVERSATION_CONTRACT_VERSION = 1
EXACT_CONVERSATION = re.compile(r"/c/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}/?$", re.I)

OBS_SOURCES = {"browser", "ledger", "receipt", "watchdog", "human"}
INTENT_SOURCES = {"human", "watchdog", "coordinator"}
BODY = {"unknown", "empty", "incomplete", "substantive"}
OBS_DELIVERY = {"unknown", "none", "pending", "delivered", "uncertain"}
STATE_DELIVERY = {"none", "pending", "delivered", "uncertain"}
PROGRESS = {"idle", "active", "blocked", "terminal", "unknown"}
GATES = {"none", "human_required"}
WRITER_MODES = {"managed", "legacy"}
ACTIONS = {"continue", "open_child", "stop"}
ALLOCATIONS = {"NEW", "REUSE"}

OBS_KEYS = {"contractVersion", "source", "conversationId", "target", "observedAt", "turnId", "userMessageId", "assistantMessageId", "assistantText", "readable", "generating", "terminal", "body", "humanGate", "delivery", "requestId"}
STATE_KEYS = {"contractVersion", "conversationId", "target", "stateVersion", "turn", "progress", "body", "delivery", "gate", "writer"}
TURN_KEYS = {"turnId", "userMessageId", "assistantMessageId"}
WRITER_KEYS = {"mode", "epoch"}
INTENT_KEYS = {"contractVersion", "intentId", "source", "conversationId", "target", "expectedStateVersion", "expectedWriterEpoch", "action", "allocation", "text", "expected"}
EXPECTED_KEYS = {"userMessageId", "assistantMessageId"}


def _obj(value: Any, keys: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    if set(value) != keys:
        raise ValueError(f"{label} has unexpected fields")
    return value


def _version(value: Any) -> int:
    if value != 1:
        raise ValueError("unsupported contractVersion")
    return 1


def _enum(value: Any, allowed: set[str], label: str) -> str:
    if value not in allowed:
        raise ValueError(f"invalid {label}")
    return value


def _str(value: Any, label: str, nullable: bool = False, max_length: int = 16_384) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ValueError(f"{label} must be a non-empty string within its length bound")
    return value


def _text(value: Any, label: str, nullable: bool = False, max_length: int = 1_000_000) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or len(value) > max_length:
        raise ValueError(f"{label} must be a string within its length bound")
    return value


def _int(value: Any, label: str, nullable: bool = False) -> int | None:
    if nullable and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _bool(value: Any, label: str) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    raise ValueError(f"{label} must be boolean or null")


def _url(value: Any, label: str, exact: bool = False, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    text = _str(value, label)
    parsed = urlsplit(text)
    if parsed.scheme != "https" or parsed.hostname != "chatgpt.com" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"{label} must be a ChatGPT URL")
    if exact and not EXACT_CONVERSATION.search(parsed.path):
        raise ValueError(f"{label} must identify an exact conversation")
    return text


def _instant(value: Any) -> str:
    text = _str(value, "observedAt")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError as exc:
        raise ValueError("observedAt must be an offset-aware ISO instant") from exc
    if parsed.tzinfo is None:
        raise ValueError("observedAt must be an offset-aware ISO instant")
    return text


@dataclass(frozen=True)
class ContractValue:
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        import copy
        return copy.deepcopy(self.data)

    def __getattr__(self, name: str) -> Any:
        key = {
            "progress": "progress",
            "body": "body",
            "delivery": "delivery",
            "allocation": "allocation",
            "state_version": "stateVersion",
        }.get(name)
        if key is None:
            raise AttributeError(name)
        return self.data[key]


def parse_observation(value: Mapping[str, Any]) -> ContractValue:
    obj = _obj(value, OBS_KEYS, "observation")
    _version(obj["contractVersion"])
    _enum(obj["source"], OBS_SOURCES, "observation source")
    _str(obj["conversationId"], "conversationId")
    _url(obj["target"], "target")
    _instant(obj["observedAt"])
    for key in ("turnId", "userMessageId", "assistantMessageId", "requestId"):
        _str(obj[key], key, nullable=True)
    _text(obj["assistantText"], "assistantText", nullable=True)
    if not isinstance(obj["readable"], bool):
        raise ValueError("readable must be boolean")
    _bool(obj["generating"], "generating")
    _bool(obj["terminal"], "terminal")
    _enum(obj["body"], BODY, "body")
    _bool(obj["humanGate"], "humanGate")
    _enum(obj["delivery"], OBS_DELIVERY, "delivery")
    return ContractValue(dict(obj))


def parse_conversation_state(value: Mapping[str, Any]) -> ContractValue:
    obj = _obj(value, STATE_KEYS, "conversation state")
    turn = _obj(obj["turn"], TURN_KEYS, "conversation state turn")
    writer = _obj(obj["writer"], WRITER_KEYS, "conversation state writer")
    _version(obj["contractVersion"])
    _str(obj["conversationId"], "conversationId")
    _url(obj["target"], "target")
    _int(obj["stateVersion"], "stateVersion")
    for key in TURN_KEYS:
        _str(turn[key], key, nullable=True)
    _enum(obj["progress"], PROGRESS, "progress")
    _enum(obj["body"], BODY, "body")
    _enum(obj["delivery"], STATE_DELIVERY, "delivery")
    _enum(obj["gate"], GATES, "gate")
    _enum(writer["mode"], WRITER_MODES, "writer mode")
    _int(writer["epoch"], "writer epoch")
    return ContractValue(dict(obj))


def _existing(obj: Mapping[str, Any], expected: Mapping[str, Any], require_ids: bool) -> None:
    _str(obj["conversationId"], "conversationId")
    _url(obj["target"], "target", exact=True)
    _int(obj["expectedStateVersion"], "expectedStateVersion")
    _int(obj["expectedWriterEpoch"], "expectedWriterEpoch")
    for key in EXPECTED_KEYS:
        _str(expected[key], f"expected {key} identity", nullable=not require_ids)


def parse_intent_envelope(value: Mapping[str, Any]) -> ContractValue:
    obj = _obj(value, INTENT_KEYS, "intent")
    expected = _obj(obj["expected"], EXPECTED_KEYS, "intent expected")
    _version(obj["contractVersion"])
    _str(obj["intentId"], "intentId")
    _enum(obj["source"], INTENT_SOURCES, "intent source")
    _str(obj["conversationId"], "conversationId", nullable=True)
    _url(obj["target"], "target", nullable=True)
    _int(obj["expectedStateVersion"], "expectedStateVersion", nullable=True)
    _int(obj["expectedWriterEpoch"], "expectedWriterEpoch", nullable=True)
    action = _enum(obj["action"], ACTIONS, "intent action")
    if obj["allocation"] is not None:
        _enum(obj["allocation"], ALLOCATIONS, "allocation")
    _str(obj["text"], "text", nullable=True)

    if action == "open_child" and obj["allocation"] == "NEW":
        if any(obj[key] is not None for key in ("conversationId", "target", "expectedStateVersion", "expectedWriterEpoch")) or any(expected[key] is not None for key in EXPECTED_KEYS):
            raise ValueError("NEW child intent cannot target an existing conversation")
        _str(obj["text"], "text")
    elif action == "open_child" and obj["allocation"] == "REUSE":
        _existing(obj, expected, True)
        _str(obj["text"], "text")
    elif action == "open_child":
        raise ValueError("open_child requires NEW or REUSE allocation")
    else:
        if obj["allocation"] is not None:
            raise ValueError(f"{action} intent allocation must be null")
        if action == "continue":
            _existing(obj, expected, True)
            _str(obj["text"], "text")
        else:
            _existing(obj, expected, False)
            if obj["text"] is not None:
                raise ValueError("stop intent text must be null")

    return ContractValue(dict(obj))
