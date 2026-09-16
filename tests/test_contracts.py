import json
from pathlib import Path

import pytest

from chat_watchdog.contracts import (
    CONVERSATION_CONTRACT_VERSION,
    parse_observation,
    parse_conversation_state,
    parse_intent_envelope,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "conversation-runtime-v1.json").read_text(encoding="utf-8"))


def test_v1_fixtures_parse_strictly_and_round_trip():
    assert CONVERSATION_CONTRACT_VERSION == 1
    observation = parse_observation(FIXTURE["observation"])
    state = parse_conversation_state(FIXTURE["state"])
    existing = parse_intent_envelope(FIXTURE["existingIntent"])
    new = parse_intent_envelope(FIXTURE["newIntent"])
    assert observation.to_dict() == FIXTURE["observation"]
    assert state.to_dict() == FIXTURE["state"]
    assert existing.to_dict() == FIXTURE["existingIntent"]
    assert new.to_dict() == FIXTURE["newIntent"]


@pytest.mark.parametrize(
    ("parser", "fixture_name"),
    [
        (parse_observation, "observation"),
        (parse_conversation_state, "state"),
        (parse_intent_envelope, "existingIntent"),
    ],
)
def test_unknown_versions_and_extra_fields_fail_closed(parser, fixture_name):
    candidate = dict(FIXTURE[fixture_name])
    candidate["contractVersion"] = 2
    with pytest.raises(ValueError, match="version|contractVersion"):
        parser(candidate)

    candidate = dict(FIXTURE[fixture_name])
    candidate["unexpected"] = True
    with pytest.raises(ValueError, match="field|unexpected|keys"):
        parser(candidate)


def test_existing_mutation_requires_state_version_writer_epoch_and_identity():
    for key in ("expectedStateVersion", "expectedWriterEpoch"):
        candidate = json.loads(json.dumps(FIXTURE["existingIntent"]))
        candidate[key] = None
        with pytest.raises(ValueError, match="State|state|Epoch|epoch|existing"):
            parse_intent_envelope(candidate)

    candidate = json.loads(json.dumps(FIXTURE["existingIntent"]))
    candidate["target"] = None
    with pytest.raises(ValueError, match="target|existing"):
        parse_intent_envelope(candidate)

    candidate = json.loads(json.dumps(FIXTURE["existingIntent"]))
    candidate["expected"]["userMessageId"] = None
    with pytest.raises(ValueError, match="message|identity|expected"):
        parse_intent_envelope(candidate)


def test_new_and_reuse_are_unambiguous():
    new_with_target = json.loads(json.dumps(FIXTURE["newIntent"]))
    new_with_target["target"] = FIXTURE["existingIntent"]["target"]
    with pytest.raises(ValueError, match="NEW|target|conversation"):
        parse_intent_envelope(new_with_target)

    reuse = json.loads(json.dumps(FIXTURE["existingIntent"]))
    reuse["intentId"] = "intent-reuse"
    reuse["action"] = "open_child"
    reuse["allocation"] = "REUSE"
    assert parse_intent_envelope(reuse).allocation == "REUSE"

    reuse["target"] = None
    with pytest.raises(ValueError, match="REUSE|target|conversation"):
        parse_intent_envelope(reuse)

    continue_with_allocation = json.loads(json.dumps(FIXTURE["existingIntent"]))
    continue_with_allocation["allocation"] = "REUSE"
    with pytest.raises(ValueError, match="allocation|continue"):
        parse_intent_envelope(continue_with_allocation)


def test_unknown_state_stays_explicitly_unknown():
    unknown = json.loads(json.dumps(FIXTURE["state"]))
    unknown["stateVersion"] += 1
    unknown["progress"] = "unknown"
    unknown["body"] = "unknown"
    unknown["delivery"] = "uncertain"
    parsed = parse_conversation_state(unknown)
    assert parsed.progress == "unknown"
    assert parsed.body == "unknown"
    assert parsed.delivery == "uncertain"
