"""A fresh normal binding may observe, but cannot use the bind click to send."""
from test_sidecar_simple_watchdog import Owner, sample, watcher


def test_rebind_probe_observes_terminal_turn_without_sending_review():
    owner = Owner([sample(text="action result")])
    runtime = watcher(owner)
    assert hasattr(runtime, "observe_once"), "rebind needs an observation-only first tick"
    before = runtime.durable_state
    runtime.observe_once()
    assert owner.reads == 1
    assert owner.intents == []
    assert runtime.durable_state == before
    assert runtime.diagnostics["observation_readable"] is True


def test_rebind_probe_does_not_confirm_an_unidentified_turn():
    owner = Owner([sample(assistant=None, terminal=False, body="empty", text="")])
    runtime = watcher(owner)
    runtime.observe_once()
    assert runtime.diagnostics.get("normal_probe_complete") is False
    assert runtime.state == "waiting_for_assistant"
    assert owner.intents == []


def test_done_survives_rebind_but_fresh_observation_is_still_taken():
    owner = Owner([sample(text="SUPERVISOR_DONE")])
    runtime = watcher(owner)
    runtime.restore_state({**runtime.durable_state, "status": "DONE"})
    assert hasattr(runtime, "observe_once")
    runtime.observe_once()
    assert owner.reads == 1
    assert owner.intents == []
    assert runtime.durable_state["status"] == "DONE"
    assert runtime.state == "done"


def test_need_input_latch_survives_disposable_watcher_recreation():
    owner = Owner([sample(text="Please approve.\n[SUPERVISOR_STATE: NEED_INPUT]", gate=True)])
    old = watcher(owner)
    assert old.step() == "need_input"
    fresh = watcher(owner)
    fresh.restore_state(old.durable_state)
    assert fresh._need_input_latched is True
    assert fresh._need_input_user_turn_id == old._need_input_user_turn_id
    assert hasattr(fresh, "observe_once")
    fresh.observe_once()
    assert fresh._need_input_latched is True
    assert owner.intents == []
