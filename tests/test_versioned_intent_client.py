import hashlib
import json

import pytest

from chat_watchdog.contracts import parse_conversation_state
from chat_watchdog.intent_client import SidecarIntentClient

TARGET = "https://chatgpt.com/c/00000000-0000-0000-0000-000000000011"


def state(**overrides):
    value = {
        "contractVersion": 1,
        "conversationId": "conv-state",
        "target": TARGET,
        "stateVersion": 9,
        "turn": {
            "turnId": "turn-9",
            "userMessageId": "user-9",
            "assistantMessageId": "assistant-9",
        },
        "progress": "blocked",
        "body": "incomplete",
        "delivery": "delivered",
        "gate": "none",
        "writer": {"mode": "managed", "epoch": 3},
    }
    for key, nested in overrides.items():
        if key == "turn":
            value["turn"] = {**value["turn"], **nested}
        elif key == "writer":
            value["writer"] = {**value["writer"], **nested}
        else:
            value[key] = nested
    return parse_conversation_state(value)


def expected_intent_id(text):
    material = [
        "conversation-runtime/v1",
        "watchdog",
        "continue",
        TARGET,
        "conv-state",
        9,
        3,
        "user-9",
        "assistant-9",
        text,
    ]
    encoded = json.dumps(material, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def test_submit_v1_builds_strict_envelope_from_authoritative_state():
    calls = []

    def request(endpoint, payload):
        calls.append((endpoint, payload))
        return {"accepted": True, "conversationId": "conv-state", "turnId": "turn-next"}

    client = SidecarIntentClient(request_json=request)
    text = "continue bounded task"
    result = client.submit_v1(state(), text)

    assert result["accepted"] is True
    assert len(calls) == 1
    endpoint, payload = calls[0]
    assert endpoint.endswith("/internal/conversation-intents")
    assert payload == {
        "contractVersion": 1,
        "intentId": expected_intent_id(text),
        "source": "watchdog",
        "conversationId": "conv-state",
        "target": TARGET,
        "expectedStateVersion": 9,
        "expectedWriterEpoch": 3,
        "action": "continue",
        "allocation": None,
        "text": text,
        "expected": {
            "userMessageId": "user-9",
            "assistantMessageId": "assistant-9",
        },
    }


def test_submit_v1_intent_identity_is_stable_for_retry_and_changes_with_command():
    payloads = []

    def request(_endpoint, payload):
        payloads.append(payload)
        return {"accepted": False, "reason": "pacing"}

    client = SidecarIntentClient(request_json=request)
    authoritative = state()
    client.submit_v1(authoritative, "continue bounded task")
    client.submit_v1(authoritative, "continue bounded task")
    client.submit_v1(authoritative, "different continuation")

    assert payloads[0]["intentId"] == payloads[1]["intentId"]
    assert payloads[0]["intentId"] != payloads[2]["intentId"]


def test_submit_v1_rejects_missing_authoritative_message_identity_before_post():
    client = SidecarIntentClient(request_json=lambda *_: pytest.fail("must not post invalid intent"))
    with pytest.raises(ValueError):
        client.submit_v1(state(turn={"assistantMessageId": None}), "continue bounded task")


def test_submit_v1_rejects_invalid_owner_response():
    client = SidecarIntentClient(request_json=lambda *_: {"accepted": "yes"})
    with pytest.raises(RuntimeError, match="invalid intent owner receipt"):
        client.submit_v1(state(), "continue bounded task")
