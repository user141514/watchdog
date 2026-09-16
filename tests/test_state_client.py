import json

import pytest

from chat_watchdog.state_client import SidecarStateClient, StateProtocolError, StateUnavailable

TARGET = "https://chatgpt.com/g/g-p-6a983ccfa9148191b42da3db5412f946-subagents/c/00000000-0000-0000-0000-000000000001"
STATE = {
    "contractVersion": 1,
    "conversationId": "conv_state",
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


def test_state_client_reads_strict_v1_state_for_exact_target():
    calls = []

    def request(endpoint, payload):
        calls.append((endpoint, payload))
        return {"found": True, "state": json.loads(json.dumps(STATE))}

    endpoint = "http://127.0.0.1:7337/internal/conversation-state"
    client = SidecarStateClient(endpoint, request_json=request)
    state = client.read(TARGET)
    assert state.state_version == 9
    assert state.progress == "blocked"
    assert state.to_dict() == STATE
    assert calls == [(endpoint, {"target": TARGET})]


def test_state_client_fails_closed_for_unavailable_target_and_transport():
    missing = SidecarStateClient(
        "http://localhost:7337/internal/conversation-state",
        request_json=lambda *_: {"found": False, "reason": "target_unavailable"},
    )
    with pytest.raises(StateUnavailable, match="target_unavailable"):
        missing.read(TARGET)

    failed = SidecarStateClient(
        "http://localhost:7337/internal/conversation-state",
        request_json=lambda *_: (_ for _ in ()).throw(TimeoutError("sidecar down")),
    )
    with pytest.raises(StateUnavailable, match="sidecar down"):
        failed.read(TARGET)


def test_state_client_classifies_malformed_json_as_protocol_error():
    client = SidecarStateClient(
        "http://127.0.0.1:7337/internal/conversation-state",
        request_json=lambda *_: (_ for _ in ()).throw(json.JSONDecodeError("bad json", "{", 1)),
    )
    with pytest.raises(StateProtocolError, match="invalid JSON"):
        client.read(TARGET)


def test_state_client_rejects_malformed_future_and_wrong_target_responses():
    cases = [
        {"found": True, "state": {**STATE, "contractVersion": 2}},
        {"found": True, "state": {**STATE, "unexpected": True}},
        {"found": True, "state": {**STATE, "target": "https://chatgpt.com/c/00000000-0000-0000-0000-000000000002"}},
        {"found": "yes", "state": STATE},
    ]
    for payload in cases:
        client = SidecarStateClient(
            "http://127.0.0.1:7337/internal/conversation-state",
            request_json=lambda *_args, payload=payload: payload,
        )
        with pytest.raises(StateProtocolError):
            client.read(TARGET)


def test_state_client_rejects_nonlocal_owner_and_nonexact_target():
    with pytest.raises(ValueError, match="localhost"):
        SidecarStateClient("https://remote.example/internal/conversation-state")
    client = SidecarStateClient(
        "http://127.0.0.1:7337/internal/conversation-state",
        request_json=lambda *_: pytest.fail("must not call owner"),
    )
    with pytest.raises(ValueError, match="exact conversation"):
        client.read("https://chatgpt.com/")
