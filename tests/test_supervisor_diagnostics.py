from chat_watchdog.cli import _SupervisorWatcher
from chat_watchdog.registry import WatchRegistry
from chat_watchdog.state_client import StateUnavailable
from chat_watchdog.supervisor import StepResult, Supervisor
from test_supervisor_authoritative_state import Intents, Page, Pool, States, TARGET, state


def test_owner_outage_is_distinguishable_from_an_alive_idle_watcher():
    page = Page()
    page.close = lambda: None
    sup = Supervisor(page, Pool(), intent_client=Intents(),
                     state_client=States(error=StateUnavailable("sidecar unreachable")))
    watcher = _SupervisorWatcher(page, sup)
    registry = WatchRegistry(lambda url: watcher)
    try:
        registry.register(TARGET)
        registry.step_all()
        details = registry.list()[0].diagnostics
        assert details["decision"] == "waiting"
        assert details["reason"] == "state_owner_unavailable"
        assert details["state_available"] is False
        assert "sidecar unreachable" in details["error"]
    finally:
        registry.close()


def test_relay_unavailable_sentinel_is_not_a_real_snapshot():
    sup = Supervisor(Page(assistant="relay-unavailable"), Pool(), intent_client=Intents(),
                     state_client=States(result=state(delivery="uncertain", progress="unknown", body="unknown")))
    assert sup.step() is StepResult.DELIVERY_UNCERTAIN
    assert sup.diagnostics["snapshot_available"] is False


def test_uncertain_delivery_remains_visible_and_never_forces_a_send():
    intents = Intents()
    sup = Supervisor(Page(), Pool(), intent_client=intents,
                     state_client=States(result=state(delivery="uncertain", progress="unknown", body="unknown")))
    assert sup.step() is StepResult.DELIVERY_UNCERTAIN
    assert sup.diagnostics["state_available"] is True
    assert sup.diagnostics["delivery"] == "uncertain"
    assert sup.diagnostics["progress"] == "unknown"
    assert sup.diagnostics["writer_epoch"] == 3
    assert intents.calls == []


def test_fresh_observation_clears_prior_owner_error_and_reports_intent_receipt():
    states = States(error=StateUnavailable("brief outage"))
    sup = Supervisor(Page(), Pool(), intent_client=Intents(), state_client=states)
    assert sup.step() is StepResult.WAITING
    states.error = None
    states.result = state()
    assert sup.step() is StepResult.CONTINUED
    assert sup.diagnostics["error"] is None
    assert sup.diagnostics["state_version"] == 9
    assert sup.diagnostics["intent_accepted"] is True
    assert sup.diagnostics["intent_reason"] is None
