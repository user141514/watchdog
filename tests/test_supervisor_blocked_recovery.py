from __future__ import annotations

import unittest

from chat_watchdog.model import PageSnapshot, Phase
from chat_watchdog.supervisor import CONTINUE_PROMPT, StepResult, Supervisor


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FakePage:
    target_url = "https://chatgpt.com/c/00000000-0000-0000-0000-000000000001"

    def __init__(self, snapshot: PageSnapshot, outcomes: list[bool] | None = None) -> None:
        self.current = snapshot
        self.sent: list[tuple[str, tuple[int, str]]] = []
        self.outcomes = None if outcomes is None else list(outcomes)

    def snapshot(self) -> PageSnapshot:
        return self.current

    def send_continue(self, prompt: str, expected_turn_key: tuple[int, str]) -> bool:
        self.sent.append((prompt, expected_turn_key))
        if self.outcomes is None:
            return True
        return self.outcomes.pop(0) if self.outcomes else False


class NoAgentPool:
    candidate_names: tuple[str, ...] = ()

    def try_acquire(self, prompt: str, exclude=()):
        return None


def active_snapshot(
    *,
    signature: str = "sig-1",
    text: str = "partial assistant response",
    assistant_turn_id: str = "assistant-1",
    assistant_count: int = 1,
    user_count: int = 1,
    user_turn_id: str = "user-1",
) -> PageSnapshot:
    return PageSnapshot(
        phase=Phase.RESPONDING,
        assistant_turn_id=assistant_turn_id,
        assistant_text_signature=signature,
        assistant_text=text,
        assistant_count=assistant_count,
        user_count=user_count,
        user_turn_id=user_turn_id,
    )


def blocked_snapshot(
    *,
    signature: str = "sig-1",
    text: str = "partial assistant response",
    interaction_required: bool = False,
    assistant_turn_id: str = "assistant-1",
    assistant_count: int = 1,
    user_count: int = 1,
    user_turn_id: str = "user-1",
    send_timeout: bool = False,
    stream_interrupted: bool = False,
) -> PageSnapshot:
    return PageSnapshot(
        phase=Phase.BLOCKED,
        assistant_turn_id=assistant_turn_id,
        assistant_text_signature=signature,
        assistant_text=text,
        assistant_count=assistant_count,
        user_count=user_count,
        user_turn_id=user_turn_id,
        interaction_required=interaction_required,
        send_timeout=send_timeout,
        stream_interrupted=stream_interrupted,
    )


class BlockedRecoveryTests(unittest.TestCase):
    def test_stable_partial_blocked_turn_waits_359_then_continues_once_at_360(self) -> None:
        clock = FakeClock()
        page = FakePage(blocked_snapshot())
        supervisor = Supervisor(page, NoAgentPool(), recovery_timeout_seconds=360.0, clock=clock)

        self.assertEqual(supervisor.step(), StepResult.BLOCKED)
        clock.now = 359.0
        self.assertEqual(supervisor.step(), StepResult.BLOCKED)
        self.assertEqual(page.sent, [])

        clock.now = 360.0
        self.assertEqual(supervisor.step(), StepResult.CONTINUED)
        self.assertEqual(page.sent, [(CONTINUE_PROMPT, page.current.turn_key)])

        clock.now = 720.0
        self.assertEqual(supervisor.step(), StepResult.ALREADY_HANDLED)
        self.assertEqual(len(page.sent), 1)

    def test_rejected_liveness_attempt_consumes_same_assistant_epoch(self) -> None:
        clock = FakeClock()
        page = FakePage(active_snapshot(), outcomes=[False, True])
        supervisor = Supervisor(page, NoAgentPool(), clock=clock)

        self.assertEqual(supervisor.step(), StepResult.ACTIVE)
        clock.now = 360.0
        self.assertEqual(supervisor.step(), StepResult.BLOCKED)
        self.assertEqual(len(page.sent), 1)

        clock.now = 720.0
        self.assertEqual(supervisor.step(), StepResult.ACTIVE)
        self.assertEqual(len(page.sent), 1)

    def test_verified_assistant_progress_opens_new_liveness_epoch(self) -> None:
        clock = FakeClock()
        page = FakePage(active_snapshot(signature="sig-1"), outcomes=[True, True])
        supervisor = Supervisor(page, NoAgentPool(), clock=clock)

        self.assertEqual(supervisor.step(), StepResult.ACTIVE)
        clock.now = 360.0
        self.assertEqual(supervisor.step(), StepResult.CONTINUED)

        clock.now = 361.0
        page.current = active_snapshot(signature="sig-2", text="more partial output")
        self.assertEqual(supervisor.step(), StepResult.ACTIVE)
        clock.now = 720.0
        self.assertEqual(supervisor.step(), StepResult.ACTIVE)
        clock.now = 721.0
        self.assertEqual(supervisor.step(), StepResult.CONTINUED)
        self.assertEqual(len(page.sent), 2)

    def test_user_only_change_never_opens_new_liveness_epoch(self) -> None:
        clock = FakeClock()
        page = FakePage(active_snapshot(user_count=1, user_turn_id="user-1"), outcomes=[True, True])
        supervisor = Supervisor(page, NoAgentPool(), clock=clock)

        self.assertEqual(supervisor.step(), StepResult.ACTIVE)
        clock.now = 360.0
        self.assertEqual(supervisor.step(), StepResult.CONTINUED)

        page.current = active_snapshot(user_count=2, user_turn_id="watchdog-continuation")
        clock.now = 361.0
        self.assertEqual(supervisor.step(), StepResult.ACTIVE)
        clock.now = 721.0
        self.assertEqual(supervisor.step(), StepResult.ACTIVE)
        self.assertEqual(len(page.sent), 1)

    def test_done_and_need_input_preempt_liveness_timeout(self) -> None:
        cases = (
            (blocked_snapshot(text="finished\nSUPERVISOR_DONE"), StepResult.DONE),
            (blocked_snapshot(text="waiting\n[SUPERVISOR_STATE: NEED_INPUT]"), StepResult.NEED_INPUT),
            (blocked_snapshot(interaction_required=True), StepResult.NEED_INPUT),
        )
        for snapshot, expected in cases:
            with self.subTest(expected=expected):
                clock = FakeClock()
                page = FakePage(snapshot)
                supervisor = Supervisor(page, NoAgentPool(), clock=clock)
                self.assertEqual(supervisor.step(), expected)
                clock.now = 1200.0
                self.assertEqual(supervisor.step(), expected)
                self.assertEqual(page.sent, [])

    def test_empty_or_invalid_identity_never_auto_continues(self) -> None:
        cases = (
            blocked_snapshot(text=""),
            blocked_snapshot(assistant_turn_id="relay-unavailable", assistant_count=0),
        )
        for snapshot in cases:
            with self.subTest(snapshot=snapshot):
                clock = FakeClock()
                page = FakePage(snapshot)
                supervisor = Supervisor(page, NoAgentPool(), clock=clock)
                self.assertEqual(supervisor.step(), StepResult.BLOCKED)
                clock.now = 1200.0
                self.assertEqual(supervisor.step(), StepResult.BLOCKED)
                self.assertEqual(page.sent, [])

        clock = FakeClock()
        page = FakePage(blocked_snapshot())
        page.target_url = "https://chatgpt.com/"
        supervisor = Supervisor(page, NoAgentPool(), clock=clock)
        self.assertEqual(supervisor.step(), StepResult.BLOCKED)
        clock.now = 1200.0
        self.assertEqual(supervisor.step(), StepResult.BLOCKED)
        self.assertEqual(page.sent, [])

    def test_stable_active_generation_waits_359_then_liveness_continues_at_360(self) -> None:
        clock = FakeClock()
        active = PageSnapshot(
            phase=Phase.RESPONDING,
            assistant_turn_id="assistant-1",
            assistant_text_signature="sig-1",
            assistant_text="still generating",
            assistant_count=1,
            user_count=1,
            user_turn_id="user-1",
        )
        page = FakePage(active)
        supervisor = Supervisor(page, NoAgentPool(), clock=clock)
        self.assertEqual(supervisor.step(), StepResult.ACTIVE)
        clock.now = 359.0
        self.assertEqual(supervisor.step(), StepResult.ACTIVE)
        self.assertEqual(page.sent, [])
        clock.now = 360.0
        self.assertEqual(supervisor.step(), StepResult.CONTINUED)
        self.assertEqual(page.sent, [(CONTINUE_PROMPT, page.current.turn_key)])

    def test_active_need_input_marker_preempts_liveness_timeout(self) -> None:
        clock = FakeClock()
        page = FakePage(PageSnapshot(
            phase=Phase.RESPONDING,
            assistant_turn_id="assistant-1",
            assistant_text_signature="sig-need-input",
            assistant_text="Waiting for backend choice.\n[SUPERVISOR_STATE: NEED_INPUT]",
            assistant_count=1,
            user_count=1,
            user_turn_id="user-1",
        ))
        supervisor = Supervisor(page, NoAgentPool(), clock=clock)
        self.assertEqual(supervisor.step(), StepResult.NEED_INPUT)
        clock.now = 1200.0
        self.assertEqual(supervisor.step(), StepResult.NEED_INPUT)
        self.assertEqual(page.sent, [])

    def test_active_assistant_progress_resets_liveness_deadline(self) -> None:
        clock = FakeClock()
        page = FakePage(PageSnapshot(
            phase=Phase.RESPONDING,
            assistant_turn_id="assistant-1",
            assistant_text_signature="sig-1",
            assistant_text="partial one",
            assistant_count=1,
            user_count=1,
            user_turn_id="user-1",
        ))
        supervisor = Supervisor(page, NoAgentPool(), clock=clock)
        self.assertEqual(supervisor.step(), StepResult.ACTIVE)
        clock.now = 359.0
        page.current = PageSnapshot(
            phase=Phase.RESPONDING,
            assistant_turn_id="assistant-1",
            assistant_text_signature="sig-2",
            assistant_text="partial two",
            assistant_count=1,
            user_count=1,
            user_turn_id="user-1",
        )
        self.assertEqual(supervisor.step(), StepResult.ACTIVE)
        clock.now = 718.0
        self.assertEqual(supervisor.step(), StepResult.ACTIVE)
        self.assertEqual(page.sent, [])
        clock.now = 719.0
        self.assertEqual(supervisor.step(), StepResult.CONTINUED)
        self.assertEqual(len(page.sent), 1)

    def test_failed_liveness_attempt_does_not_fall_into_recovery(self) -> None:
        clock = FakeClock()
        page = FakePage(active_snapshot(), outcomes=[False])

        class CountingPool(NoAgentPool):
            def __init__(self) -> None:
                self.calls = 0

            def try_acquire(self, prompt: str, exclude=()):
                self.calls += 1
                return None

        pool = CountingPool()
        supervisor = Supervisor(page, pool, clock=clock)
        self.assertEqual(supervisor.step(), StepResult.ACTIVE)
        clock.now = 360.0
        self.assertEqual(supervisor.step(), StepResult.BLOCKED)
        page.current = PageSnapshot(
            phase=Phase.FINISHED,
            assistant_turn_id="assistant-1",
            assistant_text_signature="sig-1",
            assistant_text="partial assistant response",
            assistant_count=1,
            user_count=2,
            user_turn_id="late-user-turn",
        )
        clock.now = 361.0
        self.assertEqual(supervisor.step(), StepResult.ALREADY_HANDLED)
        self.assertEqual(pool.calls, 0)


if __name__ == "__main__":
    unittest.main()
