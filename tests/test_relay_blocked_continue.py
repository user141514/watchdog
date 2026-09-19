from __future__ import annotations

import unittest

from chat_watchdog.model import PageSnapshot, Phase
from chat_watchdog.relay_page import RelayChatGPTPage


def blocked_snapshot() -> PageSnapshot:
    return PageSnapshot(
        phase=Phase.BLOCKED,
        assistant_turn_id="assistant-7",
        assistant_text_signature="stable-output",
        assistant_text="Work is incomplete but this turn stopped.",
        assistant_count=7,
        user_count=3,
    )


class FakeProtocol:
    def __init__(self) -> None:
        self.evaluate_calls = 0
        self.expressions: list[str] = []

    def evaluate(self, session_id: str, expression: str, await_promise: bool = False):
        self.evaluate_calls += 1
        self.expressions.append(expression)
        return {"submitted": True}


class BlockedRelayContinueTests(unittest.TestCase):
    def test_send_liveness_continue_runs_dom_submit_for_active_snapshot(self) -> None:
        protocol = FakeProtocol()
        page = RelayChatGPTPage(
            target_id="page-1",
            target_url="https://chatgpt.com/c/test",
            session_id="session-1",
            socket=object(),
            protocol=protocol,
            match_url="/c/test",
        )
        before = PageSnapshot(
            phase=Phase.RESPONDING,
            assistant_turn_id="assistant-7",
            assistant_text_signature="stable-output",
            assistant_text="still generating but unchanged",
            assistant_count=7,
            user_count=3,
            user_turn_id="user-3",
        )
        after = PageSnapshot(
            phase=Phase.THINKING,
            assistant_turn_id="assistant-7",
            assistant_text_signature="stable-output",
            assistant_text="still generating but unchanged",
            assistant_count=7,
            user_count=4,
            user_turn_id="user-4",
        )
        snapshots = iter([before, after])
        page.snapshot = lambda: next(snapshots)

        self.assertTrue(page.send_liveness_continue("continue", before.turn_key, acceptance_timeout=0.2))
        self.assertEqual(protocol.evaluate_calls, 1)
        self.assertIn("const allowGenerationActive = true;", protocol.expressions[0])

    def test_ordinary_continue_still_does_not_submit_during_active_generation(self) -> None:
        protocol = FakeProtocol()
        page = RelayChatGPTPage(
            target_id="page-1",
            target_url="https://chatgpt.com/c/test",
            session_id="session-1",
            socket=object(),
            protocol=protocol,
            match_url="/c/test",
        )
        active = PageSnapshot(
            phase=Phase.RESPONDING,
            assistant_turn_id="assistant-7",
            assistant_text_signature="stable-output",
            assistant_text="still generating",
            assistant_count=7,
            user_count=3,
            user_turn_id="user-3",
        )
        page.snapshot = lambda: active

        self.assertTrue(page.send_continue("continue", active.turn_key, acceptance_timeout=0.2))
        self.assertEqual(protocol.evaluate_calls, 0)

    def test_send_continue_runs_dom_guard_for_blocked_snapshot(self) -> None:
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
