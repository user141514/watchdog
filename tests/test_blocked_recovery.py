import unittest

from chat_watchdog.model import PageSnapshot, Phase
from chat_watchdog.relay_page import RelayChatGPTPage
from chat_watchdog.supervisor import StepResult, Supervisor


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakePage:
    target_url = "https://chatgpt.com/c/test"

    def __init__(self, snapshot: PageSnapshot, outcomes: list[bool]) -> None:
        self.current = snapshot
        self.outcomes = list(outcomes)
        self.continue_calls = 0

    def snapshot(self) -> PageSnapshot:
        return self.current

    def send_continue(self, prompt: str, expected_turn_key: tuple[int, str]) -> bool:
        self.continue_calls += 1
        return self.outcomes.pop(0) if self.outcomes else False


class EmptyAgentPool:
    candidate_names: tuple[str, ...] = ()

    def try_acquire(self, prompt: str, exclude=()):
        return None


def blocked_snapshot() -> PageSnapshot:
    return PageSnapshot(
        phase=Phase.BLOCKED,
        assistant_turn_id="assistant-7",
        assistant_text_signature="stable-output",
        assistant_text="Work is incomplete but this turn stopped.",
        assistant_count=7,
        user_count=3,
    )


class BlockedSupervisorRecoveryTests(unittest.TestCase):
    def test_stable_blocked_turn_continues_after_recovery_timeout(self):
        clock = FakeClock()
        page = FakePage(blocked_snapshot(), [True])
        supervisor = Supervisor(
            page,
            EmptyAgentPool(),
            recovery_timeout_seconds=10.0,
            clock=clock,
        )

        self.assertEqual(supervisor.step(), StepResult.BLOCKED)
        clock.advance(11.0)

        self.assertEqual(supervisor.step(), StepResult.CONTINUED)
        self.assertEqual(page.continue_calls, 1)

    def test_failed_blocked_continue_is_retried_on_a_later_poll(self):
        clock = FakeClock()
        page = FakePage(blocked_snapshot(), [False, True])
        supervisor = Supervisor(
            page,
            EmptyAgentPool(),
            recovery_timeout_seconds=10.0,
            clock=clock,
        )

        self.assertEqual(supervisor.step(), StepResult.BLOCKED)
        clock.advance(11.0)
        self.assertEqual(supervisor.step(), StepResult.BLOCKED)
        self.assertEqual(page.continue_calls, 1)

        clock.advance(1.0)
        self.assertEqual(supervisor.step(), StepResult.CONTINUED)
        self.assertEqual(page.continue_calls, 2)


class FakeProtocol:
    def __init__(self) -> None:
        self.evaluate_calls = 0

    def evaluate(self, session_id: str, expression: str, await_promise: bool = False):
        self.evaluate_calls += 1
        return {"submitted": True}


class BlockedRelayContinueTests(unittest.TestCase):
    def test_send_continue_runs_dom_guard_for_blocked_snapshot(self):
        protocol = FakeProtocol()
        page = RelayChatGPTPage(
            target_id="page-1",
            target_url="https://chatgpt.com/c/test",
            session_id="session-1",
            socket=object(),
            protocol=protocol,
            match_url="/c/test",
        )
        before = blocked_snapshot()
        after = PageSnapshot(
            phase=Phase.THINKING,
            assistant_turn_id="assistant-7",
            assistant_text_signature="stable-output",
            assistant_text="Work is incomplete but this turn stopped.",
            assistant_count=7,
            user_count=4,
            user_turn_id="user-4",
        )
        snapshots = iter([before, after])
        page.snapshot = lambda: next(snapshots)

        self.assertTrue(page.send_continue("continue", before.turn_key, acceptance_timeout=0.2))
        self.assertEqual(protocol.evaluate_calls, 1)


if __name__ == "__main__":
    unittest.main()
