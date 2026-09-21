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
    def test_send_simple_continue_allows_pending_user_turn_and_active_ui(self) -> None:
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
            assistant_text="stale old assistant",
            assistant_count=7,
            user_count=4,
            user_turn_id="user-pending",
            user_turn_pending=True,
        )
        after = PageSnapshot(
            phase=Phase.RESPONDING,
            assistant_turn_id="assistant-7",
            assistant_text_signature="stable-output",
            assistant_text="stale old assistant",
            assistant_count=7,
            user_count=5,
            user_turn_id="user-watchdog",
            user_turn_pending=True,
            submission_receipt_seq=1,
            submission_receipt_id="user-watchdog",
            submission_receipt_text="continue",
        )
        snapshots = iter([before, after])
        page.snapshot = lambda: next(snapshots)

        self.assertTrue(page.send_simple_continue("continue", before.turn_key, acceptance_timeout=0.2))
        self.assertIn("const allowGenerationActive = true;", protocol.expressions[0])
        self.assertIn("const allowUserTurnPending = true;", protocol.expressions[0])

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
            submission_receipt_seq=1,
            submission_receipt_id="user-4",
            submission_receipt_text="continue",
        )
        snapshots = iter([before, after])
        page.snapshot = lambda: next(snapshots)

        self.assertTrue(page.send_liveness_continue("continue", before.turn_key, acceptance_timeout=0.2))
        self.assertEqual(protocol.evaluate_calls, 1)
        self.assertIn("const allowGenerationActive = true;", protocol.expressions[0])

    def test_send_prompt_accepts_new_user_message_id_despite_virtualized_counts(self) -> None:
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
            phase=Phase.FINISHED,
            assistant_turn_id="assistant-old",
            assistant_text_signature="stable-output",
            assistant_text="finished",
            assistant_count=17,
            user_count=17,
            user_turn_id="user-old",
        )
        after = PageSnapshot(
            phase=Phase.THINKING,
            assistant_turn_id="assistant-old",
            assistant_text_signature="stable-output",
            assistant_text="finished",
            assistant_count=1,
            user_count=2,
            user_turn_id="user-new",
            user_turn_pending=True,
            submission_receipt_seq=1,
            submission_receipt_id="user-new",
            submission_receipt_text="new prompt",
        )
        page.snapshot = lambda: before if protocol.evaluate_calls == 0 else after

        delivery = page.send_prompt("new prompt", before.turn_key, acceptance_timeout=0.2)
        self.assertTrue(delivery.accepted)
        self.assertEqual(delivery.message_id, "user-new")
        self.assertEqual(protocol.evaluate_calls, 1)

    def test_reordered_user_id_without_submission_receipt_is_uncertain_not_accepted(self) -> None:
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
            phase=Phase.FINISHED,
            assistant_turn_id="assistant-current",
            assistant_text_signature="sig-current",
            assistant_text="done",
            assistant_count=17,
            user_count=17,
            user_turn_id="user-current",
            submission_receipt_seq=7,
        )
        reordered = PageSnapshot(
            phase=Phase.BLOCKED,
            assistant_turn_id="assistant-older",
            assistant_text_signature="sig-older",
            assistant_text="older",
            assistant_count=1,
            user_count=2,
            user_turn_id="user-older",
            user_turn_pending=True,
            submission_receipt_seq=7,
        )
        page.snapshot = lambda: before if protocol.evaluate_calls == 0 else reordered

        delivery = page.send_prompt("new prompt", before.turn_key, acceptance_timeout=0.02)
        self.assertFalse(delivery.accepted)
        self.assertTrue(delivery.uncertain)
        self.assertEqual(delivery.message_id, "")
        self.assertEqual(protocol.evaluate_calls, 1)

    def test_submission_receipt_acknowledges_exact_prompt_delivery(self) -> None:
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
            phase=Phase.FINISHED,
            assistant_turn_id="assistant-current",
            assistant_text_signature="sig-current",
            assistant_text="done",
            assistant_count=1,
            user_count=1,
            user_turn_id="user-current",
            submission_receipt_seq=7,
        )
        after = PageSnapshot(
            phase=Phase.BLOCKED,
            assistant_turn_id="assistant-current",
            assistant_text_signature="sig-current",
            assistant_text="done",
            assistant_count=1,
            user_count=2,
            user_turn_id="user-new",
            user_turn_pending=True,
            submission_receipt_seq=8,
            submission_receipt_id="user-new",
            submission_receipt_text="new prompt",
        )
        page.snapshot = lambda: before if protocol.evaluate_calls == 0 else after

        delivery = page.send_prompt("new prompt", before.turn_key, acceptance_timeout=0.2)
        self.assertTrue(delivery.accepted)
        self.assertFalse(delivery.uncertain)
        self.assertEqual(delivery.message_id, "user-new")

    def test_liveness_active_to_same_active_state_is_not_delivery_ack(self) -> None:
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
            assistant_turn_id="assistant-stable",
            assistant_text_signature="stable-output",
            assistant_text="still generating but unchanged",
            assistant_count=1,
            user_count=1,
            user_turn_id="user-stable",
        )
        page.snapshot = lambda: active

        self.assertFalse(page.send_liveness_continue("continue", active.turn_key, acceptance_timeout=0.02))
        self.assertEqual(protocol.evaluate_calls, 1)

    def test_pre_send_turn_mismatch_is_stale_not_accepted(self) -> None:
        protocol = FakeProtocol()
        page = RelayChatGPTPage(
            target_id="page-1",
            target_url="https://chatgpt.com/c/test",
            session_id="session-1",
            socket=object(),
            protocol=protocol,
            match_url="/c/test",
        )
        current = PageSnapshot(
            phase=Phase.FINISHED,
            assistant_turn_id="assistant-other",
            assistant_text_signature="sig-other",
            assistant_text="other visible turn",
            assistant_count=1,
            user_count=1,
            user_turn_id="user-other",
        )
        page.snapshot = lambda: current

        delivery = page.send_prompt("new prompt", "assistant-expected", acceptance_timeout=0.02)
        self.assertFalse(delivery.accepted)
        self.assertEqual(delivery.message_id, "")
        self.assertEqual(protocol.evaluate_calls, 0)

    def test_ordinary_continue_active_race_is_stale_not_accepted(self) -> None:
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

        delivery = page.send_continue("continue", active.turn_key, acceptance_timeout=0.2)
        self.assertFalse(delivery.accepted)
        self.assertTrue(delivery.stale)
        self.assertFalse(delivery.uncertain)
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
            submission_receipt_seq=1,
            submission_receipt_id="user-4",
            submission_receipt_text="continue",
        )
        snapshots = iter([before, after])
        page.snapshot = lambda: next(snapshots)

        self.assertTrue(page.send_continue("continue", before.turn_key, acceptance_timeout=0.2))
        self.assertEqual(protocol.evaluate_calls, 1)


if __name__ == "__main__":
    unittest.main()
