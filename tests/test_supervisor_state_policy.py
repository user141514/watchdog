from __future__ import annotations

import json
from pathlib import Path

from chat_watchdog.contracts import parse_conversation_state
from chat_watchdog.model import PageSnapshot, Phase
from chat_watchdog.supervisor import Supervisor, StepResult

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "conversation-runtime-v1.json").read_text(encoding="utf-8"))


class Page:
    target_url = FIXTURE["state"]["target"]

    def __init__(self, snapshot: PageSnapshot | None = None):
        self.current = snapshot or PageSnapshot(
            phase=Phase.BLOCKED,
            assistant_turn_id="assistant-1",
            assistant_text_signature="sig",
            assistant_text="partial",
            assistant_count=1,
            user_count=1,
            user_turn_id="user-1",
        )
        self.effects = 0

    def snapshot(self):
        return self.current

    def send_continue(self, *args):
        self.effects += 1
        raise AssertionError("managed Relay write")

    def retry_fault(self, *args):
        self.effects += 1
        raise AssertionError("managed Relay retry")


class Pool:
    calls = 0

    def try_acquire(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("managed recovery worker")


class StateClient:
    def __init__(self, state):
        self.state = parse_conversation_state(state)
        self.calls = []

    def read(self, target):
        self.calls.append(target)
        return self.state


class IntentClient:
    def __init__(self, response=None):
        self.response = response or {"accepted": True}
        self.v1_calls = []

    def submit_v1(self, state, text):
        self.v1_calls.append((state.to_dict(), text))
        return self.response

    def submit(self, *args, **kwargs):
        raise AssertionError("v0 intent path used under authoritative state")


def state(**overrides):
    value = json.loads(json.dumps(FIXTURE["state"]))
    value.update(overrides)
    return value


def supervisor(state_value, *, snapshot=None, response=None):
    page = Page(snapshot)
    pool = Pool()
    intents = IntentClient(response)
    states = StateClient(state_value)
    return Supervisor(page, pool, intent_client=intents, state_client=states), page, pool, intents, states


def test_sidecar_active_overrides_conflicting_relay_blocked_phase():
    s, page, pool, intents, states = supervisor(state(progress="active", body="incomplete"))
    assert page.current.phase is Phase.BLOCKED
    assert s.step() is StepResult.ACTIVE
    assert intents.v1_calls == []
    assert page.effects == 0
    assert pool.calls == 0
    assert states.calls == [page.target_url]


def test_unknown_and_undelivered_state_wait_without_side_effects():
    for value in [
        state(progress="unknown", body="unknown"),
        state(progress="blocked", body="incomplete", delivery="pending"),
    ]:
        s, page, pool, intents, _states = supervisor(value)
        assert s.step() is StepResult.WAITING
        assert intents.v1_calls == []
        assert page.effects == 0
        assert pool.calls == 0


def test_human_gate_and_delivery_uncertainty_are_hard_vetoes():
    gated = state(gate="human_required", progress="blocked", body="incomplete")
    s, page, pool, intents, _states = supervisor(gated)
    assert s.step() is StepResult.NEED_INPUT
    assert intents.v1_calls == []
    assert page.effects == 0
    assert pool.calls == 0

    uncertain = state(delivery="uncertain", progress="blocked", body="incomplete")
    s, page, pool, intents, _states = supervisor(uncertain)
    assert s.step() is StepResult.DELIVERY_UNCERTAIN
    assert intents.v1_calls == []
    assert page.effects == 0
    assert pool.calls == 0


def test_blocked_incomplete_state_publishes_exactly_one_v1_continuation():
    s, page, pool, intents, _states = supervisor(state(progress="blocked", body="incomplete"))
    assert s.step() is StepResult.CONTINUED
    assert len(intents.v1_calls) == 1
    published, _text = intents.v1_calls[0]
    assert published["stateVersion"] == FIXTURE["state"]["stateVersion"]
    assert published["writer"]["epoch"] == FIXTURE["state"]["writer"]["epoch"]
    assert published["turn"] == FIXTURE["state"]["turn"]
    assert page.effects == 0
    assert pool.calls == 0
    assert s.step() is StepResult.ALREADY_HANDLED
    assert len(intents.v1_calls) == 1


def test_terminal_lifecycle_is_authoritative_but_done_marker_is_exact_turn_policy_evidence():
    done_snapshot = PageSnapshot(
        phase=Phase.BLOCKED,
        assistant_turn_id="assistant-1",
        assistant_text_signature="sig-done",
        assistant_text="result\nSUPERVISOR_DONE",
        assistant_count=1,
        user_count=1,
        user_turn_id="user-1",
    )
    s, page, pool, intents, _states = supervisor(
        state(progress="terminal", body="substantive"), snapshot=done_snapshot
    )
    assert s.step() is StepResult.DONE
    assert s.should_stop is True
    assert intents.v1_calls == []
    assert page.effects == 0
    assert pool.calls == 0

    wrong_turn = PageSnapshot(
        phase=Phase.FINISHED,
        assistant_turn_id="assistant-other",
        assistant_text_signature="sig-wrong",
        assistant_text="SUPERVISOR_DONE",
        assistant_count=2,
        user_count=2,
        user_turn_id="user-other",
    )
    s, page, pool, intents, _states = supervisor(
        state(progress="terminal", body="substantive"), snapshot=wrong_turn
    )
    assert s.step() is StepResult.WAITING
    assert s.should_stop is False
    assert intents.v1_calls == []
    assert page.effects == 0
    assert pool.calls == 0
