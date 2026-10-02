from __future__ import annotations

from pathlib import Path

from chat_watchdog.model import PageSnapshot, Phase, PromptDelivery
from chat_watchdog.registry import WatchRegistry
from chat_watchdog.simple_watchdog import SimpleWatcher


URL = "https://chatgpt.com/c/00000000-0000-0000-0000-000000000123"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def snap(*, assistant_id="assistant-1", signature="sig-1", text="work remains",
         user_id="user-1", phase=Phase.FINISHED, user_pending=False):
    return PageSnapshot(
        phase=phase, assistant_turn_id=assistant_id,
        assistant_text_signature=signature, assistant_text=text,
        assistant_count=1, user_count=1, user_turn_id=user_id,
        user_turn_pending=user_pending,
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


def test_natural_finality_drives_action_review_and_repeated_turn_never_duplicates():
    clock = FakeClock()
    page = FakePage([
        snap(),
        snap(),
        snap(assistant_id="review-1", signature="review-sig",
             text='{"decision":"CONTINUE","next_prompt":"advance next step"}'),
        snap(assistant_id="action-2", signature="action-sig"),
    ])
    watcher = SimpleWatcher(URL, sleep=lambda _: None, page_factory=factory(page),
                            clock=clock, liveness_timeout_seconds=15.0)

    assert watcher.step() == "transition_sent"
    assert watcher.durable_state["phase"] == 1
    assert "REVIEW 0" in page.sent[0][0]
    clock.now = 10.0
    assert watcher.step() == "transition_pending"
    assert len(page.sent) == 1
    clock.now = 15.0
    assert watcher.step() == "transition_sent"
    assert watcher.durable_state["phase"] == 0
    assert watcher.durable_state["cycle"] == 1
    assert page.sent[1][0] == "advance next step"
    clock.now = 20.0
    assert watcher.step() == "transition_sent"
    assert "REVIEW 1" in page.sent[2][0]


def test_send_failure_retains_phase_and_retries_only_on_next_scheduler_tick():
    page = FakePage([snap(), snap(), snap()],
                    deliveries=[PromptDelivery(accepted=False),
                                PromptDelivery(accepted=True, message_id="u2")])
    watcher = SimpleWatcher(URL, sleep=lambda _: None, page_factory=factory(page))
    assert watcher.step() == "send_rejected"
    assert watcher.durable_state["phase"] == 0
    assert len(page.sent) == 1
    assert watcher.step() == "transition_sent"
    assert watcher.durable_state["phase"] == 1
    assert len(page.sent) == 2
    assert watcher.step() == "transition_pending"
    assert len(page.sent) == 2


def test_action_done_has_no_terminal_authority_but_review_done_latches():
    old_done = snap(text="finished\nSUPERVISOR_DONE", assistant_id="assistant-old")
    review_done = snap(text='{"decision":"DONE","terminal":"SUPERVISOR_DONE"}',
                       assistant_id="review-new", signature="review-new-sig")
    page = FakePage([old_done, old_done, review_done])
    watcher = SimpleWatcher(URL, sleep=lambda _: None, page_factory=factory(page))
    assert watcher.step() == "transition_sent"
    assert watcher.durable_state["status"] == "RUNNING"
    assert watcher.step() == "transition_pending"
    assert watcher.step() == "done"
    closed = page.closed
    assert watcher.step() == "done"
    assert page.closed == closed
    assert len(page.sent) == 1


def test_need_input_pauses_until_new_human_turn_then_release_tick_only_observes():
    baseline = snap(phase=Phase.RESPONDING)
    gated = snap(text="need operator\n[SUPERVISOR_STATE: NEED_INPUT]",
                 assistant_id="assistant-2", signature="sig-2")
    human = snap(text=gated.assistant_text, user_id="user-2",
                 assistant_id="assistant-2", signature="sig-2", user_pending=True)
    resumed = snap(user_id="user-2", assistant_id="assistant-3", signature="sig-3")
    page = FakePage([baseline, gated, gated, human, resumed, resumed])
    watcher = SimpleWatcher(URL, sleep=lambda _: None, page_factory=factory(page))
    assert watcher.step() == "active"
    assert watcher.step() == "need_input"
    assert watcher.step() == "need_input"
    assert watcher.step() == "human_input_observed"
    assert page.sent == []
    assert watcher.step() == "transition_sent"
    assert len(page.sent) == 1
    assert watcher.step() == "transition_pending"
    assert len(page.sent) == 1


def test_target_change_never_sends():
    page = FakePage([snap()], url="https://chatgpt.com/c/00000000-0000-0000-0000-000000000999")
    watcher = SimpleWatcher(URL, sleep=lambda _: None, page_factory=factory(page))
    assert watcher.step() == "target_changed"
    assert page.sent == []


def test_registry_keeps_done_watch_registered_but_future_polls_are_noops(tmp_path: Path):
    page = FakePage([
        snap(),
        snap(text='{"decision":"DONE","terminal":"SUPERVISOR_DONE"}',
             assistant_id="review-1", signature="review-sig"),
    ])
    registry = WatchRegistry(
        lambda url: SimpleWatcher(url, sleep=lambda _: None, page_factory=factory(page)),
        store_path=tmp_path / "simple.sqlite3",
    )
    try:
        result = registry.register(URL)
        registry.step_all()
        registry.step_all()
        assert result.created is True
        assert registry.list_ids() == ["00000000-0000-0000-0000-000000000123"]
        assert registry.list()[0].state == "done"
        closed = page.closed
        registry.step_all()
        assert page.closed == closed
        assert len(page.sent) == 1
    finally:
        registry.close()
