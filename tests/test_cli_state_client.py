import pytest

from chat_watchdog.cli import (
    build_intent_client,
    build_parser,
    build_state_client,
    validate_managed_registration,
)
from chat_watchdog.registry import RegistrationRejected
from chat_watchdog.state_client import StateProtocolError, StateUnavailable


def test_managed_cli_derives_state_and_intent_clients_from_one_sidecar_owner():
    parser = build_parser()
    args = parser.parse_args([
        "--intent-url", "http://localhost:7337/internal/conversation-intents"
    ])
    intent = build_intent_client(args)
    state = build_state_client(args)
    assert intent.endpoint == "http://localhost:7337/internal/conversation-intents"
    assert state.endpoint == "http://localhost:7337/internal/conversation-state"


def test_legacy_direct_mode_has_no_managed_state_or_intent_owner():
    parser = build_parser()
    args = parser.parse_args(["--legacy-direct-send"])
    assert build_intent_client(args) is None
    assert build_state_client(args) is None


def test_managed_registration_preflight_accepts_exact_managed_state():
    calls = []

    class State:
        def to_dict(self):
            return {"writer": {"mode": "managed"}}

    class States:
        def read(self, target):
            calls.append(target)
            return State()

    target = "https://chatgpt.com/c/00000000-0000-4000-8000-000000000001"
    validate_managed_registration(States(), target)
    assert calls == [target]


def test_managed_registration_preflight_rejects_unavailable_protocol_and_writer_mode():
    target = "https://chatgpt.com/c/00000000-0000-4000-8000-000000000001"

    class BrokenStates:
        def __init__(self, error):
            self.error = error

        def read(self, _target):
            raise self.error

    for error in (
        StateUnavailable("target_unavailable"),
        StateProtocolError("authoritative state target identity mismatch"),
    ):
        with pytest.raises(RegistrationRejected) as raised:
            validate_managed_registration(BrokenStates(error), target)
        assert raised.value.code == "NOT_MOUNTABLE_MANAGED"

    class WrongWriterStates:
        def read(self, _target):
            return type("State", (), {"to_dict": lambda self: {"writer": {"mode": "legacy"}}})()

    with pytest.raises(RegistrationRejected) as raised:
        validate_managed_registration(WrongWriterStates(), target)
    assert raised.value.code == "NOT_MOUNTABLE_MANAGED"
    assert raised.value.reason == "writer_mode_mismatch"


def test_cli_rejects_conflicting_managed_owner_configuration():
    parser = build_parser()
    args = parser.parse_args([
        "--intent-url", "http://localhost:7337/internal/conversation-intents",
        "--send-admission-url", "http://localhost:7337/internal/send-admission",
    ])
    with pytest.raises(ValueError, match="choose one Sidecar owner endpoint"):
        build_state_client(args)
