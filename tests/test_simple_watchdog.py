from __future__ import annotations

from pathlib import Path

from chat_watchdog.model import PageSnapshot, Phase, PromptDelivery
from chat_watchdog.registry import WatchRegistry
from chat_watchdog.simple_watchdog import SimpleWatcher
from chat_watchdog.supervisor import CONTINUE_PROMPT


URL = "https://chatgpt.com/c/00000000-0000-0000-0000-000000000123"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def snap(
    *,
    assistant_id="assistant-1",
    signature="sig-1",
    text="work remains",
    user_id="user-1",
    phase=Phase.FINISHED,
):
    return PageSnapshot(
        phase=phase,
        assistant_turn_id=assistant_id,
        assistant_text_signature=signature,
        assistant_text=text,
        assistant_count=1,
        user_count=1,
        user_turn_id=user_id,
    )


class FakePage:
    def __init__(self, snapshots, *, url=URL, deliveries=None):
        self.url = url
        self.snapshots = list(snapshots)
        self.last = self.snapshots[0]
        self.sent = []
        self.closed = 0
        self.deliveries = list(deliveries or [PromptDelivery(accepted=True, message_id="u-watch")])

    def current_url(self):
        return self.url

    def snapshot(self):
        if self.snapshots:
            self.last = self.snapshots.pop(0)
        return self.last

    def send_simple_continue(self, prompt, expected_turn_key, acceptance_timeout=90.0):
        self.sent.append((prompt, expected_turn_key, acceptance_timeout))
        if self.deliveries:
            return self.deliveries.pop(0)
        return PromptDelivery(accepted=True, message_id=f"u-watch-{len(self.sent)}")

    def close(self):
        self.closed += 1


def factory(page):
    return lambda _relay, _match: page


def test_fixed_timer_ignores_progress_and_sends_only_when_due():
    clock = FakeClock()
    page = FakePage([
        snap(signature="sig-1"),
        snap(signature="sig-2", assistant_id="assistant-2"),
        snap(signature="sig-3", assistant_id="assistant-3"),
        snap(signature="sig-4", assistant_id="assistant-4"),
        snap(signature="sig-5", assistant_id="assistant-5"),
    ])
    watcher = SimpleWatcher(
        URL,
        sleep=lambda _: None,
        page_factory=factory(page),
        clock=clock,
        liveness_timeout_seconds=15.0,
    )

    assert watcher.step() == "timer_waiting"
    clock.now = 10.0
    assert watcher.step() == "timer_waiting"
    assert page.sent == []

    clock.now = 15.0
    assert watcher.step() == "scheduled_sent"
    assert len(page.sent) == 1

    clock.now = 20.0
    assert watcher.step() == "timer_waiting"
    assert len(page.sent) == 1

    clock.now = 30.0
    assert watcher.step() == "scheduled_sent"
    assert len(page.sent) == 2


def test_send_failure_does_not_create_rapid_retry_loop():
    clock = FakeClock()
    page = FakePage(
        [snap(), snap(), snap()],
        deliveries=[PromptDelivery(accepted=False), PromptDelivery(accepted=True, message_id="u2")],
    )
    watcher = SimpleWatcher(
        URL,
        sleep=lambda _: None,
        page_factory=factory(page),
        clock=clock,
        liveness_timeout_seconds=15.0,
    )

    clock.now = 15.0
    assert watcher.step() == "scheduled_rejected"
    assert len(page.sent) == 1

    clock.now = 16.0
    assert watcher.step() == "timer_waiting"
    assert len(page.sent) == 1

    clock.now = 30.0
    assert watcher.step() == "scheduled_sent"
    assert len(page.sent) == 2


def test_preexisting_done_is_history_but_new_done_latches():
    clock = FakeClock()
    old_done = snap(text="finished\nSUPERVISOR_DONE", assistant_id="assistant-old")
    new_done = snap(text="finished again\nSUPERVISOR_DONE", assistant_id="assistant-new", signature="sig-new")
    page = FakePage([old_done, old_done, new_done])
    watcher = SimpleWatcher(
        URL,
        sleep=lambda _: None,
        page_factory=factory(page),
        clock=clock,
        liveness_timeout_seconds=15.0,
    )

    assert watcher.step() == "timer_waiting"
    clock.now = 15.0
    assert watcher.step() == "scheduled_sent"
    clock.now = 16.0
    assert watcher.step() == "done"
    assert len(page.sent) == 1


def test_need_input_pauses_until_new_human_turn_then_restarts_full_period():
    clock = FakeClock()
    baseline = snap(text="work remains", user_id="user-1", assistant_id="assistant-1")
    gated = snap(text="need operator\n[SUPERVISOR_STATE: NEED_INPUT]", user_id="user-1", assistant_id="assistant-2", signature="sig-2")
    human = snap(text="need operator\n[SUPERVISOR_STATE: NEED_INPUT]", user_id="user-2", assistant_id="assistant-2", signature="sig-2")
    resumed = snap(text="work remains", user_id="user-2", assistant_id="assistant-3", signature="sig-3")
    page = FakePage([baseline, gated, human, resumed, resumed])
    watcher = SimpleWatcher(
        URL,
        sleep=lambda _: None,
        page_factory=factory(page),
        clock=clock,
        liveness_timeout_seconds=15.0,
    )

    assert watcher.step() == "timer_waiting"
    clock.now = 1.0
    assert watcher.step() == "need_input"

    clock.now = 2.0
    assert watcher.step() == "human_input_observed"
    assert page.sent == []

    clock.now = 16.0
    assert watcher.step() == "timer_waiting"
    assert page.sent == []

    clock.now = 17.0
    assert watcher.step() == "scheduled_sent"
    assert len(page.sent) == 1


def test_target_change_never_sends():
    clock = FakeClock()
    page = FakePage([snap()], url="https://chatgpt.com/c/00000000-0000-0000-0000-000000000999")
    watcher = SimpleWatcher(
        URL,
        sleep=lambda _: None,
        page_factory=factory(page),
        clock=clock,
        liveness_timeout_seconds=15.0,
    )

    clock.now = 15.0
    assert watcher.step() == "target_changed"
    assert page.sent == []


def test_registry_keeps_done_watch_registered_but_future_polls_are_noops(tmp_path: Path):
    page = FakePage([
        snap(text="work remains", assistant_id="assistant-1"),
        snap(text="SUPERVISOR_DONE", assistant_id="assistant-2", signature="sig-2"),
    ])
    registry = WatchRegistry(
        lambda url: SimpleWatcher(
            url,
            sleep=lambda _: None,
            page_factory=factory(page),
            liveness_timeout_seconds=15.0,
        ),
        store_path=tmp_path / "simple.sqlite3",
    )
    try:
        result = registry.register(URL)
        registry.step_all()
        registry.step_all()
        assert result.created is True
        assert registry.list_ids() == ["00000000-0000-0000-0000-000000000123"]
        assert registry.list()[0].state == "done"
        assert page.sent == []
    finally:
        registry.close()
