"""Manual attach rebuilds the cached normal binding, not the task generation."""
from copy import deepcopy
from threading import Event, Thread

import pytest

from chat_watchdog import cli
from chat_watchdog.registry import WatchRegistry, RegistrationRejected

CID = "00000000-0000-4000-8000-000000000173"
URL = f"https://chatgpt.com/c/{CID}"
STATE = {"phase": 1, "cycle": 4, "status": "RUNNING", "next_prompt": "preserve",
         "expected_review_intent_id": "uncertain-review", "transition_turn_id": "old-turn"}


class FakeWatcher:
    should_stop = False
    completion_text = None
    state = "observation_unavailable"

    def __init__(self, events):
        self.events = events
        self.diagnostics = {"observation_available": False, "reason": "human_turn_anchor_unavailable"}
        self.durable_state = deepcopy(STATE)
        self.restored = None

    def bind_registration(self, generation):
        self.events.append(("bind", generation))

    def restore_state(self, state):
        self.restored = deepcopy(state)
        self.durable_state = deepcopy(state)

    def step(self):
        self.events.append(("normal-step", None))

    def observe_once(self):
        self.events.append(("observe-only", None))
        self.state = "observing"
        self.diagnostics = {"observation_available": True, "observation_readable": True,
                            "observation_source": "browser", "observation_observed_at": "fresh"}

    def close(self):
        self.events.append(("close", None))
        self.durable_state.clear()


def setup_registry(tmp_path):
    events, watchers = [], []
    def factory(_url):
        watcher = FakeWatcher(events)
        watchers.append(watcher)
        return watcher
    registry = WatchRegistry(factory, store_path=tmp_path / "registry.sqlite3")
    registry.register(URL)
    registry.step_all()
    events.clear()
    return registry, events, watchers


def test_manual_rebind_replaces_cache_preserves_generation_and_probes_without_send(tmp_path):
    registry, events, watchers = setup_registry(tmp_path)
    try:
        before = registry.list()[0]
        assert hasattr(registry, "request_rebind"), "manual bind needs a real cache-rebuild request"
        receipt = registry.request_rebind(CID, operation_id="op-1")
        assert receipt["status"] == "pending"
        assert registry.list()[0].state == "rebinding"
        registry.step_all()
        after = registry.list()[0]
        assert len(watchers) == 2
        assert events == [("close", None), ("bind", before.registration_id), ("observe-only", None)]
        assert after.registration_id == before.registration_id
        assert after.registered_at == before.registered_at
        assert after.last_registration == before.last_registration
        assert watchers[1].restored == STATE
        assert after.normal_binding["status"] == "observed"
        assert after.normal_binding["operation_id"] == "op-1"
        registry.step_all()
        assert events[-1][0] == "normal-step"
    finally:
        registry.close()


def test_late_rebind_cannot_touch_replacement_registration(tmp_path):
    registry, events, watchers = setup_registry(tmp_path)
    try:
        old = registry.list()[0].registration_id
        registry.unregister(CID)
        registry.register(URL)
        assert registry.list()[0].registration_id != old
        with pytest.raises(RegistrationRejected):
            registry.request_rebind(CID, operation_id="stale", expected_registration_id=old)
    finally:
        registry.close()


def test_later_poll_failure_does_not_rewrite_a_completed_binding_receipt(tmp_path):
    registry, events, watchers = setup_registry(tmp_path)
    try:
        registry.request_rebind(CID, operation_id="completed-probe")
        registry.step_all()
        receipt = deepcopy(registry.list()[0].normal_binding)
        def failure():
            raise RuntimeError("temporary normal poll error")
        watchers[-1].step = failure
        registry.step_all()
        assert registry.list()[0].last_error is not None
        assert registry.list()[0].normal_binding == receipt
        watchers[-1].step = lambda: None
        registry.step_all()
        assert registry.list()[0].last_error is None
        assert registry.list()[0].normal_binding == receipt
    finally:
        registry.close()


def test_same_operation_retry_does_not_rebuild_twice(tmp_path):
    registry, events, watchers = setup_registry(tmp_path)
    try:
        assert hasattr(registry, "request_rebind")
        registry.request_rebind(CID, operation_id="same")
        registry.step_all()
        registry.request_rebind(CID, operation_id="same")
        registry.step_all()
        assert len(watchers) == 2
    finally:
        registry.close()


def test_unregistration_wins_over_queued_rebind(tmp_path):
    registry, events, watchers = setup_registry(tmp_path)
    try:
        assert hasattr(registry, "request_rebind")
        registry.request_rebind(CID, operation_id="withdrawn")
        registry.unregister(CID)
        registry.step_all()
        assert len(watchers) == 1
        assert not registry.list()
        with pytest.raises(RegistrationRejected):
            registry.request_rebind(CID, operation_id="late")
    finally:
        registry.close()


def test_observation_unavailable_counts_as_degraded_not_success(tmp_path):
    registry, events, watchers = setup_registry(tmp_path)
    try:
        assert registry.health()["degraded_count"] == 1
        assert registry.list()[0].last_success_at is None
    finally:
        registry.close()


def test_transient_probe_failure_stays_pending_and_next_probe_can_observe(tmp_path):
    events, watchers = [], []

    class TransientWatcher(FakeWatcher):
        def __init__(self, events):
            super().__init__(events)
            self.probes = 0

        def observe_once(self):
            self.probes += 1
            self.events.append(("observe-only", self.probes))
            if self.probes == 1:
                self.state = "observation_unavailable"
                self.diagnostics = {"observation_available": False, "reason": "observation_unavailable"}
            else:
                self.state = "observing"
                self.diagnostics = {
                    "normal_probe_complete": True,
                    "observation_available": True,
                    "observation_readable": True,
                    "observation_source": "browser",
                    "observation_observed_at": "fresh",
                }

    def factory(_url):
        watcher = FakeWatcher(events) if not watchers else TransientWatcher(events)
        watchers.append(watcher)
        return watcher

    registry = WatchRegistry(factory, store_path=tmp_path / "registry.sqlite3")
    try:
        registry.register(URL)
        registry.step_all()
        registry.request_rebind(CID, operation_id="transient")
        registry.step_all()
        first = registry.list()[0]
        assert first.normal_binding["status"] == "pending"
        assert first.state == "rebinding"
        assert registry.has_pending_rebind_probe() is True

        registry.step_all()
        second = registry.list()[0]
        assert second.normal_binding["status"] == "observed"
        assert second.normal_binding["operation_id"] == "transient"
        assert second.diagnostics["normal_probe_complete"] is True
        assert registry.has_pending_rebind_probe() is False
    finally:
        registry.close()


def test_pending_rebind_uses_short_scheduler_interval():
    class Registry:
        def step_all(self):
            pass
        def has_scheduler_work(self):
            return True
        def has_pending_rebind_probe(self):
            return True
        def list(self):
            return []

    class Wait:
        timeout = None
        def clear(self):
            pass
        def wait(self, timeout=None):
            self.timeout = timeout

    wake = Wait()
    cli._registry_poll_cycle(Registry(), wake, 15)
    assert wake.timeout is not None
    assert 0 < wake.timeout <= 0.5


def test_persistent_probe_failure_becomes_unavailable_after_bounded_window(tmp_path):
    now = [100.0]
    events, watchers = [], []

    class UnavailableWatcher(FakeWatcher):
        def observe_once(self):
            self.events.append(("observe-only", None))
            self.state = "observation_unavailable"
            self.diagnostics = {"observation_available": False, "reason": "observation_unavailable"}

    def factory(_url):
        watcher = FakeWatcher(events) if not watchers else UnavailableWatcher(events)
        watchers.append(watcher)
        return watcher

    registry = WatchRegistry(
        factory,
        store_path=tmp_path / "registry.sqlite3",
        clock=lambda: now[0],
    )
    try:
        registry.register(URL)
        registry.step_all()
        registry.request_rebind(CID, operation_id="bounded")
        registry.step_all()
        assert registry.list()[0].normal_binding["status"] == "pending"

        now[0] += 6.0
        registry.step_all()
        watch = registry.list()[0]
        assert watch.normal_binding["status"] == "unavailable"
        assert watch.normal_binding["reason"] == "observation_unavailable"
        assert registry.has_pending_rebind_probe() is False
    finally:
        registry.close()


def test_rebind_failure_is_visible_and_does_not_forget_desired_registration(tmp_path):
    registry, events, watchers = setup_registry(tmp_path)
    try:
        assert hasattr(registry, "request_rebind")
        def fail(_url):
            raise RuntimeError("binding_identity_unknown")
        registry._watcher_factory = fail
        registry.request_rebind(CID, operation_id="failure")
        registry.step_all()
        watch = registry.list()[0]
        assert watch.normal_binding["status"] == "unavailable"
        assert "binding_identity_unknown" in watch.normal_binding["reason"]
        assert registry.is_active(CID)
        assert registry.health()["degraded_count"] == 1
    finally:
        registry.close()


def test_rebind_request_does_not_wait_for_blocked_normal_poll(tmp_path):
    registry, events, watchers = setup_registry(tmp_path)
    entered, release = Event(), Event()
    def blocked():
        entered.set()
        assert release.wait(3)
    watchers[0].step = blocked
    poller = Thread(target=registry.step_all)
    try:
        assert hasattr(registry, "request_rebind")
        poller.start()
        assert entered.wait(1)
        registry.request_rebind(CID, operation_id="during-poll")
        assert registry.list()[0].state == "rebinding"
        release.set()
        poller.join(2)
        assert registry.list()[0].normal_binding["status"] == "pending"
        registry.step_all()
        assert len(watchers) == 2
        assert registry.list()[0].normal_binding["status"] == "observed"
    finally:
        release.set()
        if poller.ident is not None:
            poller.join(3)
        registry.close()
