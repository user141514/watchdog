from __future__ import annotations

from dataclasses import dataclass
from threading import Event, Thread

import pytest

from chat_watchdog.registry import WatchRegistry


ID_A = "10000000-0000-4000-8000-000000000001"
ID_B = "10000000-0000-4000-8000-000000000002"
URL_A = f"https://chatgpt.com/c/{ID_A}"
URL_B = f"https://chatgpt.com/c/{ID_B}"


@dataclass
class Watcher:
    should_stop: bool = False
    state: str = "active"
    completion_text: str = "verified final result"
    steps: int = 0
    closes: int = 0
    failures: int = 0

    def step(self):
        self.steps += 1
        if self.failures:
            self.failures -= 1
            raise ConnectionError("relay disconnected")

    def close(self):
        self.closes += 1


def test_one_fault_does_not_skip_siblings_and_recovers():
    broken, healthy = Watcher(failures=1), Watcher()
    registry = WatchRegistry(lambda url: broken if url == URL_A else healthy)
    try:
        registry.register(URL_A)
        registry.register(URL_B)
        registry.step_all()
        assert healthy.steps == 1
        failed = registry.list()[0]
        assert failed.state == "degraded"
        assert failed.consecutive_failures == 1
        assert "relay disconnected" in failed.last_error
        registry.step_all()
        recovered = registry.list()[0]
        assert healthy.steps == 2
        assert recovered.consecutive_failures == 0
        assert recovered.last_error is None
        assert recovered.last_success_at is not None
    finally:
        registry.close()


def test_factory_failure_retains_desired_registration_and_retries():
    attempts = []
    watcher = Watcher()

    def factory(url):
        attempts.append(url)
        if len(attempts) == 1:
            raise ConnectionError("browser unavailable")
        return watcher

    registry = WatchRegistry(factory)
    try:
        result = registry.register(URL_A)
        assert result.created
        assert registry.is_active(ID_A)
        assert registry.list()[0].state == "reconnecting"
        registry.step_all()
        assert watcher.steps == 1
        assert registry.list()[0].state == "active"
    finally:
        registry.close()


def test_acknowledged_registration_survives_restart_without_eager_connection(tmp_path):
    path = tmp_path / "registry.sqlite3"
    first = WatchRegistry(lambda url: Watcher(), store_path=path)
    first.register(URL_A)
    first.close()
    calls = []
    recovered = WatchRegistry(lambda url: calls.append(url) or Watcher(), store_path=path)
    try:
        assert recovered.list_ids() == [ID_A]
        assert calls == []
        assert recovered.list()[0].state == "reconnecting"
        recovered.step_all()
        assert calls == [URL_A]
        assert recovered.list()[0].last_poll_at is not None
    finally:
        recovered.close()


def test_restart_with_absent_browser_does_not_delete_any_target(tmp_path):
    path = tmp_path / "registry.sqlite3"
    original = WatchRegistry(lambda url: Watcher(), store_path=path)
    original.register(URL_A)
    original.register(URL_B)
    original.close()

    def offline(url):
        raise ConnectionError("extension is reloading")

    recovered = WatchRegistry(offline, store_path=path)
    try:
        for _ in range(3):
            recovered.step_all()
        assert recovered.list_ids() == [ID_A, ID_B]
        assert all(item.consecutive_failures == 3 for item in recovered.list())
        assert all(item.state == "reconnecting" for item in recovered.list())
    finally:
        recovered.close()


def test_completion_receipt_survives_restart_until_ack(tmp_path):
    path = tmp_path / "registry.sqlite3"
    original = WatchRegistry(lambda url: Watcher(should_stop=True), store_path=path)
    original.register(URL_A)
    original.step_all()
    original.close()
    recovered = WatchRegistry(lambda url: pytest.fail("completed target was rebound"), store_path=path)
    assert recovered.list_ids() == []
    assert recovered.completion(ID_A).result == "verified final result"
    assert recovered.ack_completion(ID_A)
    recovered.close()
    again = WatchRegistry(lambda url: Watcher(), store_path=path)
    try:
        assert again.completion(ID_A) is None
    finally:
        again.close()


def test_unregister_is_durable_and_close_is_not_unregister(tmp_path):
    path = tmp_path / "registry.sqlite3"
    first = WatchRegistry(lambda url: Watcher(), store_path=path)
    first.register(URL_A)
    first.register(URL_B)
    first.unregister(ID_A)
    first.close()
    second = WatchRegistry(lambda url: Watcher(), store_path=path)
    try:
        assert second.list_ids() == [ID_B]
    finally:
        second.close()


def test_need_input_is_not_a_completion_or_an_unmount(tmp_path):
    path = tmp_path / "registry.sqlite3"
    registry = WatchRegistry(lambda url: Watcher(state="need_input"), store_path=path)
    try:
        registry.register(URL_A)
        registry.step_all()
        assert registry.list()[0].state == "need_input"
        assert registry.is_active(ID_A)
        assert registry.completion(ID_A) is None
    finally:
        registry.close()


def test_stale_poll_snapshot_cannot_step_an_unregistered_sibling():
    other = Watcher()
    registry = None

    class RemovingWatcher(Watcher):
        def step(self):
            registry.unregister(ID_B)
            super().step()

    registry = WatchRegistry(lambda url: RemovingWatcher() if url == URL_A else other)
    try:
        registry.register(URL_A)
        registry.register(URL_B)
        registry.step_all()
        assert other.steps == 0
        assert other.closes == 1
    finally:
        registry.close()


def test_control_reads_remain_responsive_during_network_step():
    entered, release, listed = Event(), Event(), Event()

    class BlockingWatcher(Watcher):
        def step(self):
            entered.set()
            assert release.wait(3)
            super().step()

    registry = WatchRegistry(lambda url: BlockingWatcher())
    registry.register(URL_A)
    poller = Thread(target=registry.step_all)
    poller.start()
    try:
        assert entered.wait(2)
        reader = Thread(target=lambda: (registry.list(), listed.set()))
        reader.start()
        assert listed.wait(1), "network work held the registry-wide control lock"
        reader.join(2)
    finally:
        release.set()
        poller.join(3)
        registry.close()


def test_unregister_waits_for_owned_step_and_prevents_future_steps():
    entered, release, unregistered = Event(), Event(), Event()

    class BlockingWatcher(Watcher):
        def step(self):
            entered.set()
            assert release.wait(3)
            super().step()

    watcher = BlockingWatcher()
    registry = WatchRegistry(lambda url: watcher)
    registry.register(URL_A)
    poller = Thread(target=registry.step_all)
    poller.start()
    assert entered.wait(2)
    remover = Thread(target=lambda: (registry.unregister(ID_A), unregistered.set()))
    remover.start()
    try:
        assert not unregistered.wait(0.1)
        release.set()
        assert unregistered.wait(2)
        poller.join(2)
        registry.step_all()
        assert watcher.steps == 1
        assert registry.list_ids() == []
    finally:
        release.set()
        poller.join(3)
        remover.join(3)
        registry.close()


def test_close_error_cannot_abort_sibling_cleanup_or_completion():
    class BadClose(Watcher):
        def close(self):
            super().close()
            raise OSError("transport already gone")

    first, second = BadClose(should_stop=True), Watcher(should_stop=True)
    registry = WatchRegistry(lambda url: first if url == URL_A else second)
    registry.register(URL_A)
    registry.register(URL_B)
    registry.step_all()
    assert registry.completion(ID_A) is not None
    assert registry.completion(ID_B) is not None
    assert second.closes == 1
    registry.close()


def test_persistent_store_has_exactly_one_live_owner(tmp_path):
    path = tmp_path / "registry.sqlite3"
    first = WatchRegistry(lambda url: Watcher(), store_path=path)
    try:
        with pytest.raises(RuntimeError, match="already.*owned"):
            WatchRegistry(lambda url: Watcher(), store_path=path)
    finally:
        first.close()
    second = WatchRegistry(lambda url: Watcher(), store_path=path)
    second.close()


def test_health_reports_poll_freshness_and_not_just_process_existence():
    times = [100.0]
    registry = WatchRegistry(lambda url: Watcher(), clock=lambda: times[0])
    try:
        registry.register(URL_A)
        assert registry.health(stale_after=30)["polling_fresh"] is False
        registry.step_all()
        assert registry.health(stale_after=30)["polling_fresh"] is True
        times[0] += 31
        assert registry.health(stale_after=30)["polling_fresh"] is False
        assert registry.health(stale_after=30)["active_count"] == 1
    finally:
        registry.close()


def test_deferred_registration_ack_does_not_wait_for_transport():
    calls = []
    registry = WatchRegistry(lambda url: calls.append(url) or Watcher(), connect_on_register=False)
    try:
        assert registry.register(URL_A).created
        assert calls == []
        assert registry.list()[0].state == "reconnecting"
        registry.step_all()
        assert calls == [URL_A]
    finally:
        registry.close()


def test_poll_racing_registration_does_not_replace_an_already_bound_watcher():
    calls = []
    registry = WatchRegistry(lambda url: calls.append(url) or Watcher())

    class PollFirst:
        first = True

        def __enter__(self):
            if self.first:
                self.first = False
                registry.step_all()

        def __exit__(self, *args):
            return False

    registry._entry_locks[ID_A] = PollFirst()
    try:
        registry.register(URL_A)
        assert calls == [URL_A], "register replaced the binding created by the poller"
    finally:
        registry.close()


def test_closed_registry_rejects_new_registrations():
    registry = WatchRegistry(lambda url: Watcher())
    registry.close()
    with pytest.raises(RuntimeError, match="closed"):
        registry.register(URL_A)
