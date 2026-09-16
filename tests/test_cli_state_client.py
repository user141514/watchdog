import pytest

from chat_watchdog.cli import build_intent_client, build_parser, build_state_client


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


def test_cli_rejects_conflicting_managed_owner_configuration():
    parser = build_parser()
    args = parser.parse_args([
        "--intent-url", "http://localhost:7337/internal/conversation-intents",
        "--send-admission-url", "http://localhost:7337/internal/send-admission",
    ])
    with pytest.raises(ValueError, match="choose one Sidecar owner endpoint"):
        build_state_client(args)
