from __future__ import annotations

from pathlib import Path

from chat_watchdog.model import PageSnapshot, Phase, PromptDelivery
from chat_watchdog.registry import WatchRegistry
from chat_watchdog.relay_page import RelayTargetSelectionError
from chat_watchdog.simple_watchdog import SimpleWatcher
from chat_watchdog.supervisor import CONTINUE_PROMPT


URL = "https://chatgpt.com/g/g-p-test/c/00000000-0000-0000-0000-000000000123"


def snap(
    *,
    assistant_id="assistant-old",
    signature="semantic-old",
    text="old answer",
    user_id="user-old",
):
    return PageSnapshot(
        phase=Phase.FINISHED,
        assistant_turn_id=assistant_id,
        assistant_text_signature=signature,
        assistant_text=text,
        assistant_count=1,
        user_count=1,
        user_turn_id=user_id,
    )


class FakePage:
    def __init__(self, snapshots, *, url=URL, delivery=None):
        self.url = url
        self.snapshots = list(snapshots)
        self.last = self.snapshots[0]
        self.refreshed = 0
        self.closed = 0
        self.sent = []
        self.delivery = delivery if delivery is not None else PromptDelivery(accepted=True, message_id="user-watchdog")

    def refresh(self):
        self.refreshed += 1

    def current_url(self):
        return self.url

    def snapshot(self):
        if self.snapshots:
            self.last = self.snapshots.pop(0)
        return self.last

    def send_simple_continue(self, prompt, expected_turn_key, acceptance_timeout=40.0):
        self.sent.append((prompt, expected_turn_key, acceptance_timeout))
        return self.delivery

    def close(self):
        self.closed += 1


def factory(page):
    return lambda _relay, _match: page


def test_existing_page_is_refreshed_and_done_only_suppresses_this_tick():
    page = FakePage([snap(text="finished\nSUPERVISOR_DONE")])
    watcher = SimpleWatcher(URL, sleep=lambda _: None, page_factory=factory(page))

    assert watcher.step() == "done"
    assert page.refreshed == 1
    assert page.sent == []
    assert watcher.should_stop is False


def test_need_input_only_suppresses_this_tick():
    page = FakePage([snap(text="need operator\n[SUPERVISOR_STATE: NEED_INPUT]")])
    watcher = SimpleWatcher(URL, sleep=lambda _: None, page_factory=factory(page))

    assert watcher.step() == "need_input"
    assert page.sent == []
    assert watcher.should_stop is False


def test_missing_page_opens_exact_url_without_immediate_refresh():
    page = FakePage([snap()], delivery=PromptDelivery(accepted=False, uncertain=True))
    calls = {"find": 0, "open": []}

    def missing(_relay, _match):
        calls["find"] += 1
        raise RelayTargetSelectionError("no matching ChatGPT target for URL substring")

    watcher = SimpleWatcher(
        URL,
        sleep=lambda _: None,
        page_factory=missing,
        open_page=lambda url, match: calls["open"].append((url, match)) or page,
    )

    assert watcher.step() == "submission_unknown"
    assert calls["open"] == [(URL, "/c/00000000-0000-0000-0000-000000000123")]
    assert page.refreshed == 0


def test_render_identity_mismatch_aborts_before_send():
    wrong = "https://chatgpt.com/c/00000000-0000-0000-0000-000000000999"
    page = FakePage([snap()], url=wrong)
    watcher = SimpleWatcher(URL, sleep=lambda _: None, page_factory=factory(page))

    assert watcher.step() == "target_changed"
    assert page.sent == []


def test_send_reuses_existing_prompt_and_visible_bottom_change_confirms_progress():
    before = snap()
    progressed = snap(
        assistant_id="assistant-new",
        signature="semantic-new",
        text="new work",
        user_id="user-watchdog",
    )
    page = FakePage([before, progressed])
    watcher = SimpleWatcher(
        URL,
        sleep=lambda _: None,
        page_factory=factory(page),
        progress_window_seconds=10,
        progress_poll_seconds=10,
    )

    assert watcher.step() == "progress_visible"
    assert page.sent == [(CONTINUE_PROMPT, "assistant-old", 90.0)]
    assert watcher.diagnostics["frontend_user_message_id"] == "user-watchdog"


def test_no_visible_progress_after_confirmed_send_remains_unknown_and_never_replays():
    before = snap()
    page = FakePage([before, before, before])
    watcher = SimpleWatcher(
        URL,
        sleep=lambda _: None,
        page_factory=factory(page),
        progress_window_seconds=20,
        progress_poll_seconds=10,
    )

    assert watcher.step() == "sent_no_visible_progress"
    assert len(page.sent) == 1


def test_uncertain_submission_never_replays():
    page = FakePage([snap()], delivery=PromptDelivery(accepted=False, uncertain=True))
    watcher = SimpleWatcher(URL, sleep=lambda _: None, page_factory=factory(page))

    assert watcher.step() == "submission_unknown"
    assert len(page.sent) == 1


def test_registry_keeps_done_watch_registered_until_external_unregistration(tmp_path: Path):
    page = FakePage([snap(text="SUPERVISOR_DONE")])
    registry = WatchRegistry(
        lambda url: SimpleWatcher(url, sleep=lambda _: None, page_factory=factory(page)),
        store_path=tmp_path / "simple.sqlite3",
    )
    try:
        result = registry.register(URL)
        registry.step_all()
        assert result.created is True
        assert registry.list_ids() == ["00000000-0000-0000-0000-000000000123"]
        assert registry.list()[0].state == "done"
    finally:
        registry.close()
