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

    def __init__(self, snapshot: PageSnapshot) -> None:
        self.current = snapshot
        self.sent: list[tuple[str, tuple[int, str]]] = []

    def snapshot(self) -> PageSnapshot:
        return self.current

    def send_continue(self, prompt: str, expected_turn_key: tuple[int, str]) -> bool:
        self.sent.append((prompt, expected_turn_key))
        return True


class NoAgentPool:
    candidate_names: tuple[str, ...] = ()

    def try_acquire(self, prompt: str, exclude=()):
        return None


def blocked_snapshot(
    *,
    signature: str = "sig-1",
    text: str = "partial assistant response",
    interaction_required: bool = False,
) -> PageSnapshot:
    return PageSnapshot(
        phase=Phase.BLOCKED,
        assistant_turn_id="assistant-1",
        assistant_text_signature=signature,
        assistant_text=text,
        assistant_count=1,
        user_count=1,
        interaction_required=interaction_required,
    )


class BlockedRecoveryTests(unittest.TestCase):
    def test_stable_partial_blocked_turn_continues_once_after_timeout(self) -> None:
        clock = FakeClock()
        page = FakePage(blocked_snapshot())
        supervisor = Supervisor(
            page,
            NoAgentPool(),
            recovery_timeout_seconds=120.0,
            clock=clock,
        )

        self.assertEqual(supervisor.step(), StepResult.BLOCKED)
        clock.now = 119.0
        self.assertEqual(supervisor.step(), StepResult.BLOCKED)
        self.assertEqual(page.sent, [])

        clock.now = 120.0
        self.assertEqual(supervisor.step(), StepResult.CONTINUED)
        self.assertEqual(page.sent, [(CONTINUE_PROMPT, page.current.turn_key)])

        clock.now = 240.0
        self.assertEqual(supervisor.step(), StepResult.ALREADY_HANDLED)
        self.assertEqual(len(page.sent), 1)

    def test_new_partial_progress_resets_blocked_timeout(self) -> None:
        clock = FakeClock()
        page = FakePage(blocked_snapshot(signature="sig-1"))
        supervisor = Supervisor(
            page,
            NoAgentPool(),
            recovery_timeout_seconds=120.0,
            clock=clock,
        )

        self.assertEqual(supervisor.step(), StepResult.BLOCKED)
        clock.now = 100.0
        page.current = blocked_snapshot(signature="sig-2", text="more partial output")
        self.assertEqual(supervisor.step(), StepResult.BLOCKED)

        clock.now = 219.0
        self.assertEqual(supervisor.step(), StepResult.BLOCKED)
        self.assertEqual(page.sent, [])
        clock.now = 220.0
        self.assertEqual(supervisor.step(), StepResult.CONTINUED)

    def test_empty_or_interactive_blocked_turn_never_auto_continues(self) -> None:
        for snapshot in (
            blocked_snapshot(text=""),
            blocked_snapshot(interaction_required=True),
        ):
            with self.subTest(snapshot=snapshot):
                clock = FakeClock()
                page = FakePage(snapshot)
                supervisor = Supervisor(
                    page,
                    NoAgentPool(),
                    recovery_timeout_seconds=120.0,
                    clock=clock,
                )
                self.assertIn(supervisor.step(), {StepResult.BLOCKED, StepResult.NEED_INPUT})
                clock.now = 500.0
                self.assertIn(supervisor.step(), {StepResult.BLOCKED, StepResult.NEED_INPUT})
                self.assertEqual(page.sent, [])


if __name__ == "__main__":
    unittest.main()
