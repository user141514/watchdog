from __future__ import annotations

import json
import sqlite3
from threading import Event, Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from chat_watchdog import cli
from chat_watchdog.registry import WatchRegistry, create_control_server

ID = "6aa542fd-708c-83ea-869a-721efd83d7f3"
URL = f"https://chatgpt.com/c/{ID}"
BIND = {
    "url": URL, "explicit": True, "source": "observatory-ui",
    "actor": "human", "operation_id": "bind-one", "reason": "manual-bind",
}


class Watcher:
    should_stop = False
    completion_text = None
    state = "active"

    def step(self):
        pass

    def close(self):
        pass


@pytest.fixture
def control(tmp_path):
    registry = WatchRegistry(lambda _url: Watcher(), store_path=tmp_path / "registry.sqlite3")
    server = create_control_server(registry, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(method, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        with urlopen(Request(
            f"http://127.0.0.1:{server.server_port}{path}", data=data, method=method,
            headers={"content-type": "application/json"},
        ), timeout=2) as response:
            return json.load(response)

    yield registry, request, tmp_path / "registry.sqlite3"
    server.shutdown()
    server.server_close()
    thread.join(2)
    registry.close()


@pytest.mark.parametrize("payload", [
    {"url": URL},
    {**BIND, "explicit": False},
    {**BIND, "source": "sidecar-auto"},
    {**BIND, "source": None},
    {**BIND, "source": []},
    {**BIND, "actor": ""},
])
def test_http_registration_requires_explicit_controller_provenance(control, payload):
    registry, request, _path = control
    with pytest.raises(HTTPError) as raised:
        request("POST", "/register", payload)
    assert raised.value.code == 409
    assert json.load(raised.value)["error"] == "explicit_bind_required"
    assert registry.list_ids() == []


def test_bind_unbind_history_survives_restart_without_resurrection(control):
    registry, request, path = control
    assert request("POST", "/register", BIND)["created"]
    registration = request("GET", "/watches")["watches"][0]["last_registration"]
    assert registration == {
        "source": "observatory-ui", "actor": "human", "operation_id": "bind-one",
        "reason": "manual-bind", "at": registration["at"],
    }
    assert request("POST", "/unregister", {
        "conversation_id": ID, "source": "observatory-ui", "actor": "human",
        "operation_id": "unbind-one", "reason": "manual-unbind",
    })["removed"]
    events = request("GET", "/lifecycle?limit=1")["events"]
    assert len(events) == 1
    assert events[0]["operation"] == "unregister"
    assert events[0]["operation_id"] == "unbind-one"
    registry.close()
    restarted = WatchRegistry(lambda _url: Watcher(), store_path=path)
    try:
        assert restarted.list_ids() == []
        history = restarted.lifecycle()
        assert [event["operation"] for event in history] == [
            "unregister", "unregister_requested", "register",
        ]
        assert history[2]["operation_id"] == "bind-one"
    finally:
        restarted.close()


def test_active_registration_provenance_restores_and_legacy_rows_remain_unknown(tmp_path):
    path = tmp_path / "registry.sqlite3"
    first = WatchRegistry(lambda _url: Watcher(), store_path=path)
    first.register(URL, provenance={key: BIND[key] for key in
                                  ("source", "actor", "operation_id", "reason")})
    first.close()
    second = WatchRegistry(lambda _url: Watcher(), store_path=path)
    try:
        assert second.list()[0].last_registration["operation_id"] == "bind-one"
        assert second.lifecycle()[0]["source"] == "observatory-ui"
    finally:
        second.close()
    # Emulate an existing row from a release that had no source metadata.
    with sqlite3.connect(path) as db:
        db.execute("UPDATE watch_records SET last_registration=NULL")
        db.execute("DELETE FROM watch_lifecycle")
    legacy = WatchRegistry(lambda _url: Watcher(), store_path=path)
    try:
        assert legacy.list()[0].last_registration is None
        assert legacy.lifecycle() == []
    finally:
        legacy.close()


def test_additive_schema_migration_preserves_legacy_binding_without_inventing_source(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("""
            CREATE TABLE watch_records (
                conversation_id TEXT PRIMARY KEY, target_url TEXT NOT NULL,
                status TEXT NOT NULL, result TEXT, registered_at REAL NOT NULL,
                last_poll_at REAL, last_success_at REAL,
                consecutive_failures INTEGER NOT NULL DEFAULT 0, last_error TEXT
            )
        """)
        db.execute(
            "INSERT INTO watch_records (conversation_id,target_url,status,registered_at) "
            "VALUES (?,?,'active',?)", (ID, URL, 123),
        )
    registry = WatchRegistry(lambda _url: Watcher(), store_path=path)
    try:
        row = registry.list()[0]
        assert row.conversation_id == ID
        assert row.registered_at == 123
        assert row.last_registration is None
        assert registry.lifecycle() == []
    finally:
        registry.close()


def test_failed_audit_commit_cannot_create_binding_without_provenance(tmp_path):
    path = tmp_path / "registry.sqlite3"
    registry = WatchRegistry(lambda _url: Watcher(), store_path=path)
    registry._store._db.execute("""
        CREATE TRIGGER reject_audit BEFORE INSERT ON watch_lifecycle
        BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END
    """)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="audit unavailable"):
            registry.register(URL)
        assert registry.list_ids() == []
        assert registry._store.load() == []
    finally:
        registry.close()


def test_unregister_during_completed_step_cannot_recreate_durable_binding(tmp_path):
    entered, release, withdrawn = Event(), Event(), Event()

    class CompletingWatcher(Watcher):
        should_stop = True

        def step(self):
            entered.set()
            assert release.wait(3)

    path = tmp_path / "registry.sqlite3"
    registry = WatchRegistry(lambda _url: CompletingWatcher(), store_path=path)
    registry.register(URL)
    poller = Thread(target=registry.step_all)
    remover = Thread(target=lambda: (registry.unregister(ID), withdrawn.set()))
    poller.start()
    try:
        assert entered.wait(2)
        remover.start()
        assert not withdrawn.wait(0.1)
        release.set()
        assert withdrawn.wait(2)
        poller.join(2)
        remover.join(2)
        assert registry.list_ids() == []
        assert registry.completion(ID) is None
        assert registry._store.load() == []
        assert [event["operation"] for event in registry.lifecycle()] == [
            "unregister", "unregister_requested", "register",
        ]
        registry.close()
        restarted = WatchRegistry(lambda _url: Watcher(), store_path=path)
        try:
            assert restarted.list_ids() == []
            assert restarted.completion(ID) is None
        finally:
            restarted.close()
    finally:
        release.set()
        poller.join(3)
        if remover.ident is not None:
            remover.join(3)
        registry.close()


def test_failed_withdrawal_journal_commit_leaves_registration_active(tmp_path):
    registry = WatchRegistry(lambda _url: Watcher(), store_path=tmp_path / "registry.sqlite3")
    try:
        registry.register(URL)
        registry._store._db.execute("""
            CREATE TRIGGER reject_withdrawal BEFORE INSERT ON watch_lifecycle
            WHEN NEW.operation='unregister_requested'
            BEGIN SELECT RAISE(ABORT, 'withdrawal audit unavailable'); END
        """)
        with pytest.raises(sqlite3.IntegrityError, match="withdrawal audit unavailable"):
            registry.unregister(ID)
        assert registry.list_ids() == [ID]
        assert registry._store.load_withdrawals() == []
        assert registry._store.load()[0]["status"] == "active"
    finally:
        registry.close()


def test_remote_timeout_cannot_ack_unregister_and_pending_withdrawal_survives_restart(tmp_path):
    path = tmp_path / "registry.sqlite3"
    calls = []

    def unavailable(generation, target):
        calls.append((generation, target))
        raise TimeoutError("owner outcome unknown")

    registry = WatchRegistry(lambda _url: Watcher(), store_path=path,
                             withdrawal_callback=unavailable)
    registry.register(URL)
    generation = registry.list()[0].registration_id
    with pytest.raises(RuntimeError, match="withdrawal_unconfirmed"):
        registry.unregister(ID)
    assert registry.list_ids() == []
    assert registry.health()["pending_withdrawal_count"] == 1
    assert registry.lifecycle()[0]["operation"] == "unregister_requested"
    registry.close()

    def confirmed(generation, target):
        calls.append((generation, target))
        return {"accepted": True, "quiescent": True, "registrationId": generation}

    restarted = WatchRegistry(lambda _url: Watcher(), store_path=path,
                              withdrawal_callback=confirmed)
    try:
        assert restarted.list_ids() == []
        assert restarted.unregister(ID)
        assert calls == [(generation, URL), (generation, URL)]
        assert restarted.health()["pending_withdrawal_count"] == 0
        assert restarted.lifecycle()[0]["operation"] == "unregister"
    finally:
        restarted.close()


def test_bind_after_unconfirmed_withdrawal_is_rejected_until_owner_quiescent(tmp_path):
    registry = WatchRegistry(lambda _url: Watcher(), store_path=tmp_path / "registry.sqlite3",
                             withdrawal_callback=lambda _generation, _target: {"accepted": False})
    try:
        registry.register(URL)
        with pytest.raises(RuntimeError, match="withdrawal_unconfirmed"):
            registry.unregister(ID)
        with pytest.raises(RuntimeError, match="withdrawal_pending"):
            registry.register(URL)
        assert registry.list_ids() == []
    finally:
        registry.close()


def test_http_unregister_unknown_owner_effect_returns_503_and_can_be_retried(control):
    registry, request, _path = control
    registry._withdrawal_callback = lambda _generation, _target: {"accepted": False}
    request("POST", "/register", BIND)
    generation = registry.list()[0].registration_id
    with pytest.raises(HTTPError) as raised:
        request("POST", "/unregister", {"conversation_id": ID})
    assert raised.value.code == 503
    assert json.load(raised.value)["error"] == "withdrawal_unconfirmed"
    assert request("GET", "/watches")["watches"] == []
    assert request("GET", "/lifecycle")["events"][0]["operation"] == "unregister_requested"
    registry._withdrawal_callback = lambda identifier, _target: {
        "accepted": True, "quiescent": True, "registrationId": identifier,
    }
    assert request("POST", "/unregister", {"conversation_id": ID})["removed"]
    assert generation is not None


def test_effect_transition_reservation_is_durable_before_dispatch(tmp_path):
    effects = []

    class ReservingWatcher(Watcher):
        def bind_registration(self, identifier):
            self.identifier = identifier

        def set_persistence_callback(self, callback):
            self.persist = callback

        def step(self):
            self.persist({"phase": "review", "transition_turn_id": "a1"})
            effects.append(self.identifier)
            raise TimeoutError("lost effect acknowledgement")

    path = tmp_path / "registry.sqlite3"
    registry = WatchRegistry(lambda _url: ReservingWatcher(), store_path=path)
    try:
        registry.register(URL)
        registry.step_all()
        row = registry._store.load()[0]
        assert effects == [row["registration_id"]]
        assert row["runtime_state"] == {"phase": "review", "transition_turn_id": "a1"}
    finally:
        registry.close()


def test_failed_effect_reservation_prevents_dispatch(tmp_path):
    effects = []

    class ReservingWatcher(Watcher):
        def set_persistence_callback(self, callback):
            self.persist = callback

        def step(self):
            self.persist({"phase": "review"})
            effects.append("dispatched")

    registry = WatchRegistry(lambda _url: ReservingWatcher(), store_path=tmp_path / "registry.sqlite3")
    try:
        registry.register(URL)
        registry._store._db.execute("""
            CREATE TRIGGER reject_state BEFORE UPDATE ON watch_records
            WHEN NEW.runtime_state IS NOT NULL
            BEGIN SELECT RAISE(ABORT, 'state unavailable'); END
        """)
        registry.step_all()
        assert effects == []
        assert registry._store.load()[0]["runtime_state"] is None
    finally:
        registry.close()


def test_lifecycle_query_is_bounded(control):
    _registry, request, _path = control
    for limit in (0, 201, "invalid"):
        with pytest.raises(HTTPError) as raised:
            request("GET", f"/lifecycle?limit={limit}")
        assert raised.value.code == 400


def test_empty_registry_health_stays_ready_without_timer():
    now = [10.0]
    registry = WatchRegistry(lambda _url: Watcher(), clock=lambda: now[0])
    try:
        registry.step_all()
        now[0] = 100000.0
        assert registry.health()["ready"]
        assert registry.health()["polling_fresh"]
        registry.register(URL)
        assert not registry.health()["ready"]
    finally:
        registry.close()


@pytest.mark.parametrize("fail", [False, True])
def test_control_server_exit_wakes_empty_scheduler(fail):
    wake = Event()

    class Server:
        def serve_forever(self, *, poll_interval):
            if fail:
                raise RuntimeError("control stopped")

    if fail:
        with pytest.raises(RuntimeError, match="control stopped"):
            cli._serve_registry_control(Server(), wake)
    else:
        cli._serve_registry_control(Server(), wake)
    assert wake.is_set()


def test_scheduler_does_not_lose_control_exit_before_clearing_wake():
    class Registry:
        def step_all(self):
            raise AssertionError("control already exited")

    wake = Event()
    wake.set()
    cli._registry_poll_cycle(Registry(), wake, 15, is_control_alive=lambda: False)


def test_pending_remote_withdrawal_retries_when_owner_recovers_without_new_user_action(tmp_path):
    recovered = [False]

    def withdraw(generation, _target):
        if not recovered[0]:
            raise TimeoutError("owner unavailable")
        return {"accepted": True, "quiescent": True, "registrationId": generation}

    registry = WatchRegistry(lambda _url: Watcher(), store_path=tmp_path / "registry.sqlite3",
                             withdrawal_callback=withdraw)

    class Wait:
        timeout = None

        def clear(self):
            pass

        def wait(self, timeout=None):
            self.timeout = timeout

    wake = Wait()
    try:
        registry.register(URL)
        with pytest.raises(RuntimeError, match="withdrawal_unconfirmed"):
            registry.unregister(ID)
        cli._registry_poll_cycle(registry, wake, 0.1)
        assert wake.timeout is not None
        recovered[0] = True
        cli._registry_poll_cycle(registry, wake, 0.1)
        assert not registry.has_scheduler_work()
        assert wake.timeout is None
        assert registry.health()["ready"]
    finally:
        registry.close()


def test_empty_scheduler_wait_has_no_timeout_and_preserves_racing_wake():
    class RacingRegistry:
        def step_all(self):
            pass

        def has_scheduler_work(self):
            wake.set()
            return False

    class RecordingEvent:
        signaled = False
        timeout = "unset"

        def clear(self):
            self.signaled = False

        def set(self):
            self.signaled = True

        def wait(self, timeout=None):
            self.timeout = timeout
            assert self.signaled, "registration wake was cleared after membership check"

    wake = RecordingEvent()
    cli._registry_poll_cycle(RacingRegistry(), wake, 15)
    assert wake.timeout is None


def test_explicit_register_materializes_mechanical_before_success_and_wakes_normal(tmp_path):
    events = []
    registry = WatchRegistry(lambda _url: Watcher(), store_path=tmp_path / "registry.sqlite3")

    def projection(target_url):
        assert target_url == URL
        assert registry.list_ids() == [ID]
        events.append("mechanical")

    server = create_control_server(
        registry,
        port=0,
        wake=lambda: events.append("normal"),
        register_projection=projection,
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        data = json.dumps(BIND).encode()
        with urlopen(Request(
            f"http://127.0.0.1:{server.server_port}/register",
            data=data,
            method="POST",
            headers={"content-type": "application/json"},
        ), timeout=2) as response:
            payload = json.load(response)
        assert payload["created"] is True
        assert payload["mechanical_attached"] is True
        assert events == ["mechanical", "normal"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        registry.close()


def test_register_projection_failure_keeps_desired_membership_wakes_normal_and_returns_503(tmp_path):
    wake = Event()
    registry = WatchRegistry(lambda _url: Watcher(), store_path=tmp_path / "registry.sqlite3")

    def projection(_target_url):
        raise RuntimeError("task scheduler unavailable")

    server = create_control_server(
        registry,
        port=0,
        wake=wake.set,
        register_projection=projection,
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        data = json.dumps(BIND).encode()
        with pytest.raises(HTTPError) as raised:
            urlopen(Request(
                f"http://127.0.0.1:{server.server_port}/register",
                data=data,
                method="POST",
                headers={"content-type": "application/json"},
            ), timeout=2)
        assert raised.value.code == 503
        payload = json.load(raised.value)
        assert payload["error"] == "registration_projection_unavailable"
        assert payload["accepted"] is True
        assert payload["mechanical_attached"] is False
        assert registry.list_ids() == [ID]
        assert wake.is_set()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        registry.close()


def test_reregister_rechecks_mechanical_projection_even_when_membership_already_exists(tmp_path):
    projected = []
    registry = WatchRegistry(lambda _url: Watcher(), store_path=tmp_path / "registry.sqlite3")
    server = create_control_server(
        registry,
        port=0,
        wake=lambda: None,
        register_projection=lambda target_url: projected.append(target_url),
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for _ in range(2):
            data = json.dumps(BIND).encode()
            with urlopen(Request(
                f"http://127.0.0.1:{server.server_port}/register",
                data=data,
                method="POST",
                headers={"content-type": "application/json"},
            ), timeout=2) as response:
                json.load(response)
        assert projected == [URL, URL]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        registry.close()


def test_first_explicit_registration_wakes_empty_scheduler_immediately():
    entered = Event()

    class WaitingEvent(Event):
        def wait(self, timeout=None):
            entered.set()
            return super().wait(timeout)

    wake = WaitingEvent()
    registry = WatchRegistry(lambda _url: Watcher())
    sleeper = Thread(target=cli._registry_poll_cycle, args=(registry, wake, 900))
    sleeper.start()
    try:
        assert entered.wait(2)
        registry.register(URL)
        wake.set()
        sleeper.join(2)
        assert not sleeper.is_alive()
    finally:
        wake.set()
        sleeper.join(2)
        registry.close()
