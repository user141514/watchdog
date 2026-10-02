"""A lost owner binding can recover only from the same active Registry generation."""
from __future__ import annotations

from threading import Event, Thread

import pytest

from chat_watchdog.registry import WatchRegistry

ID = "6aa542fd-708c-83ea-869a-721efd83d7f3"
URL = f"https://chatgpt.com/c/{ID}"
LINEAGE = {
    "phase": 1, "cycle": 4, "next_prompt": "Keep the accepted review frontier",
    "status": "review_pending", "transition_turn_id": "assistant-action",
    "review_binding": {
        "intent_id": "accepted-review-intent", "request_user_id": "review-user",
        "assistant_turn_id": "review-assistant",
    },
}


class Owner:
    epoch = 1

    def __init__(self):
        self.grants = {}
        self.revoked = set()
        self.binds = []
        self.withdraw_unknown = False

    def bind(self, registration_id):
        if registration_id in self.revoked:
            raise RuntimeError("watchdog_registration_revoked")
        self.binds.append((registration_id, self.epoch))
        self.grants[registration_id] = self.epoch

    def withdraw(self, registration_id, _target):
        self.revoked.add(registration_id)
        self.grants.pop(registration_id, None)
        return {
            "accepted": not self.withdraw_unknown,
            "quiescent": not self.withdraw_unknown,
            "registrationId": registration_id,
        }


class Watcher:
    should_stop = False
    completion_text = None
    state = "watching"

    def __init__(self, owner):
        self.owner = owner
        self.reason = None
        self.closed = 0
        self.steps = 0
        self.on_step = None
        self.runtime_state = dict(LINEAGE)
        self.restored = None

    def bind_registration(self, registration_id):
        self.owner.bind(registration_id)
        self.registration_id = registration_id

    @property
    def requires_owner_rebind(self):
        return self.reason == "watchdog_binding_required"

    @property
    def durable_state(self):
        return dict(self.runtime_state)

    def restore_state(self, state):
        self.restored = dict(state)
        self.runtime_state = dict(state)

    def step(self):
        self.steps += 1
        if self.owner.grants.get(self.registration_id) != self.owner.epoch:
            self.reason = "watchdog_binding_required"
        else:
            self.reason = None
        if self.on_step is not None:
            self.on_step()

    def close(self):
        self.closed += 1


def registry_and_cache(tmp_path, owner):
    created = []

    def factory(_url):
        watcher = Watcher(owner)
        created.append(watcher)
        return watcher

    registry = WatchRegistry(factory, store_path=tmp_path / "registry.sqlite3",
                             withdrawal_callback=owner.withdraw)
    return registry, created


def test_owner_epoch_change_rebinds_same_desired_generation_without_new_registration(tmp_path):
    owner = Owner()
    registry, created = registry_and_cache(tmp_path, owner)
    try:
        registry.register(URL, provenance={"source": "controlled-api", "actor": "human",
                                         "operation_id": "original-bind", "reason": "explicit bind"})
        before = registry.list()[0]
        owner.epoch = 2
        registry.step_all()
        assert created[0].closed == 1
        assert registry.list()[0].connected is False
        assert registry._store.load()[0]["runtime_state"] == LINEAGE

        registry.step_all()
        after = registry.list()[0]
        assert len(created) == 2
        assert owner.binds == [(before.registration_id, 1), (before.registration_id, 2)]
        assert after.registration_id == before.registration_id
        assert after.registered_at == before.registered_at
        assert after.last_registration == before.last_registration
        assert created[1].restored == LINEAGE
        assert [event["operation"] for event in registry.lifecycle()] == ["register"]
    finally:
        registry.close()


def test_disposable_cache_cleanup_cannot_mutate_preserved_review_lineage(tmp_path):
    owner = Owner()
    created = []

    class ClearingWatcher(Watcher):
        def __init__(self, owner):
            super().__init__(owner)
            self.runtime_state["review_binding"] = dict(LINEAGE["review_binding"])

        def close(self):
            super().close()
            self.runtime_state["review_binding"].clear()

    def factory(_url):
        watcher = ClearingWatcher(owner)
        created.append(watcher)
        return watcher

    registry = WatchRegistry(factory, store_path=tmp_path / "registry.sqlite3")
    try:
        registry.register(URL)
        owner.epoch = 2
        registry.step_all()
        registry.step_all()
        assert created[1].restored == LINEAGE
        assert registry._store.load()[0]["runtime_state"] == LINEAGE
    finally:
        registry.close()


def test_other_owner_denial_does_not_evict_or_renew_binding(tmp_path):
    owner = Owner()
    registry, created = registry_and_cache(tmp_path, owner)
    try:
        registry.register(URL)
        created[0].on_step = lambda: setattr(created[0], "reason", "watchdog_registration_revoked")
        registry.step_all()
        registry.step_all()
        assert len(created) == 1
        assert created[0].closed == 0
        assert len(owner.binds) == 1
    finally:
        registry.close()


def test_pending_withdrawal_cannot_restore_stale_cache_or_owner_grant(tmp_path):
    owner = Owner()
    registry, created = registry_and_cache(tmp_path, owner)
    try:
        registry.register(URL)
        stale = registry._watchers[ID]
        owner.epoch = 2
        owner.withdraw_unknown = True
        with pytest.raises(RuntimeError, match="withdrawal_unconfirmed"):
            registry.unregister(ID)
        registry._step_entry(stale)
        registry.step_all()
        assert registry.list_ids() == []
        assert registry.health()["pending_withdrawal_count"] == 1
        assert len(created) == 1
        assert len(owner.binds) == 1
        assert owner.grants == {}
    finally:
        registry.close()


def test_unregistration_racing_binding_denial_prevents_late_cache_recovery(tmp_path):
    owner = Owner()
    registry, created = registry_and_cache(tmp_path, owner)
    entered, release, withdrawn = Event(), Event(), Event()
    poller = remover = None
    try:
        registry.register(URL)
        stale = registry._watchers[ID]
        owner.epoch = 2

        def blocked_step():
            entered.set()
            assert release.wait(3)

        created[0].on_step = blocked_step
        poller = Thread(target=registry.step_all)
        poller.start()
        assert entered.wait(2)
        remover = Thread(target=lambda: (registry.unregister(ID), withdrawn.set()))
        remover.start()
        assert not withdrawn.wait(0.1)
        release.set()
        assert withdrawn.wait(2)
        poller.join(2)
        remover.join(2)
        registry._step_entry(stale)
        registry.step_all()
        assert registry.list_ids() == []
        assert len(created) == 1
        assert len(owner.binds) == 1
        assert owner.grants == {}
    finally:
        release.set()
        if poller is not None:
            poller.join(3)
        if remover is not None:
            remover.join(3)
        registry.close()


def test_stale_generation_cannot_restore_after_new_explicit_binding(tmp_path):
    owner = Owner()
    registry, created = registry_and_cache(tmp_path, owner)
    try:
        registry.register(URL)
        stale = registry._watchers[ID]
        old_generation = stale.registration_id
        registry.unregister(ID)
        registry.register(URL)
        current = registry.list()[0]
        assert current.registration_id != old_generation
        stale.watcher.reason = "watchdog_binding_required"
        registry._step_entry(stale)
        assert len(created) == 2
        assert len(owner.binds) == 2
        assert registry.list()[0].registration_id == current.registration_id
        assert old_generation in owner.revoked
    finally:
        registry.close()


def test_phase_persistence_failure_does_not_evict_and_restore_unsaved_cache(tmp_path):
    owner = Owner()
    registry, created = registry_and_cache(tmp_path, owner)
    try:
        registry.register(URL)
        owner.epoch = 2
        registry._store._db.execute("""
            CREATE TRIGGER reject_checkpoint BEFORE UPDATE ON watch_records
            WHEN NEW.runtime_state IS NOT NULL
            BEGIN SELECT RAISE(ABORT, 'checkpoint unavailable'); END
        """)
        with pytest.raises(Exception, match="checkpoint unavailable"):
            registry.step_all()
        assert len(created) == 1
        assert created[0].closed == 0
        assert len(owner.binds) == 1
    finally:
        registry.close()
