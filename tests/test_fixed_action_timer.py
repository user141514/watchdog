import json
import tempfile
import unittest
from pathlib import Path

from chat_watchdog.fixed_action_timer import (
    FIXED_ACTION_PROMPT,
    TimerConfig,
    load_config,
    run_once,
)
from chat_watchdog.model import PageSnapshot, Phase, PromptDelivery


TARGET = "https://chatgpt.com/g/g-p-test/c/6ac12a3a-0338-83e8-a734-61c97ec8ddd6"


def snapshot(turn_key="assistant-1", *, phase=Phase.FINISHED, **extra):
    values = {
        "phase": phase,
        "assistant_turn_id": turn_key,
        "assistant_text_signature": "sig",
        "assistant_text": "done",
        "assistant_count": 1,
        "user_count": 1,
        "user_turn_id": "user-1",
    }
    values.update(extra)
    return PageSnapshot(**values)


class FakePage:
    def __init__(self, snap, delivery):
        self._snap = snap
        self.delivery = delivery
        self.calls = []
        self.closed = False

    def snapshot(self):
        return self._snap

    def send_continue(self, prompt, expected_turn_key, acceptance_timeout=3.0):
        self.calls.append(("continue", prompt, expected_turn_key, acceptance_timeout))
        return self.delivery

    def send_simple_continue(self, prompt, expected_turn_key, acceptance_timeout=40.0):
        self.calls.append(("simple", prompt, expected_turn_key, acceptance_timeout))
        return self.delivery

    def recover_stalled_active(
        self,
        prompt,
        expected_turn_key,
        *,
        stop_timeout=10.0,
        acceptance_timeout=40.0,
    ):
        self.calls.append(
            (
                "recover",
                prompt,
                expected_turn_key,
                stop_timeout,
                acceptance_timeout,
            )
        )
        return self.delivery

    def close(self):
        self.closed = True


class FixedActionTimerTests(unittest.TestCase):
    def test_prompt_is_direct_action_not_review_and_restores_simple_v2_markers(self):
        self.assertIn("直接继续执行当前任务", FIXED_ACTION_PROMPT)
        self.assertIn("不要 REVIEW", FIXED_ACTION_PROMPT)
        self.assertIn("SUPERVISOR_DONE", FIXED_ACTION_PROMPT)
        self.assertIn("[SUPERVISOR_STATE: NEED_INPUT]", FIXED_ACTION_PROMPT)
        self.assertNotIn("不要输出 SUPERVISOR_DONE", FIXED_ACTION_PROMPT)
        self.assertNotIn("REVIEW 0", FIXED_ACTION_PROMPT)
        self.assertNotIn("REVIEW 1", FIXED_ACTION_PROMPT)

    def test_load_config_requires_exact_conversation_url_and_positive_timeout(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "timer.json"
            path.write_text(
                json.dumps(
                    {
                        "target_url": TARGET,
                        "relay_url": "http://127.0.0.1:9224",
                        "acceptance_timeout_seconds": 12.0,
                    }
                ),
                encoding="utf-8",
            )
            config = load_config(path)
            self.assertEqual(config.target_url, TARGET)
            self.assertEqual(config.acceptance_timeout_seconds, 12.0)

            path.write_text(
                json.dumps(
                    {
                        "target_url": "https://chatgpt.com/",
                        "relay_url": "http://127.0.0.1:9224",
                        "acceptance_timeout_seconds": 12.0,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_config(path)

    def test_finished_turn_directly_sends_on_bound_conversation(self):
        page = FakePage(snapshot(), PromptDelivery(accepted=True, message_id="m1"))
        calls = []

        def factory(relay_url, target_url):
            calls.append((relay_url, target_url))
            return page

        result = run_once(
            TimerConfig(
                target_url=TARGET,
                relay_url="http://127.0.0.1:9224",
                acceptance_timeout_seconds=7.5,
            ),
            page_factory=factory,
        )

        self.assertEqual(calls, [("http://127.0.0.1:9224", TARGET)])
        self.assertEqual(
            page.calls,
            [("continue", FIXED_ACTION_PROMPT, "assistant-1", 7.5)],
        )
        self.assertEqual(result, {"status": "sent", "message_id": "m1"})
        self.assertTrue(page.closed)

    def test_active_turn_prefers_direct_active_send(self):
        page = FakePage(
            snapshot(phase=Phase.RESPONDING, stop_visible=False),
            PromptDelivery(accepted=True, message_id="m2"),
        )

        result = run_once(
            TimerConfig(target_url=TARGET, acceptance_timeout_seconds=7.5),
            page_factory=lambda relay_url, target_url: page,
        )

        self.assertEqual(
            page.calls,
            [("simple", FIXED_ACTION_PROMPT, "assistant-1", 40.0)],
        )
        self.assertEqual(result, {"status": "sent", "message_id": "m2"})
        self.assertTrue(page.closed)

    def test_active_direct_rejection_falls_back_to_stop_recovery(self):
        class FallbackPage(FakePage):
            def send_simple_continue(self, prompt, expected_turn_key, acceptance_timeout=40.0):
                self.calls.append(("simple", prompt, expected_turn_key, acceptance_timeout))
                return PromptDelivery(accepted=False, reason="direct_send_rejected")

            def recover_stalled_active(
                self,
                prompt,
                expected_turn_key,
                *,
                stop_timeout=10.0,
                acceptance_timeout=40.0,
            ):
                self.calls.append(
                    ("recover", prompt, expected_turn_key, stop_timeout, acceptance_timeout)
                )
                return PromptDelivery(accepted=True, message_id="m3")

        page = FallbackPage(
            snapshot(phase=Phase.RESPONDING, stop_visible=True),
            PromptDelivery(accepted=False),
        )

        result = run_once(
            TimerConfig(target_url=TARGET, acceptance_timeout_seconds=7.5),
            page_factory=lambda relay_url, target_url: page,
        )

        self.assertEqual(
            page.calls,
            [
                ("simple", FIXED_ACTION_PROMPT, "assistant-1", 40.0),
                ("recover", FIXED_ACTION_PROMPT, "assistant-1", 7.5, 40.0),
            ],
        )
        self.assertEqual(result, {"status": "sent", "message_id": "m3"})

    def test_active_uncertain_direct_send_never_falls_back(self):
        page = FakePage(
            snapshot(phase=Phase.RESPONDING),
            PromptDelivery(
                accepted=False,
                uncertain=True,
                reason="causal_receipt_timeout",
            ),
        )

        result = run_once(
            TimerConfig(target_url=TARGET),
            page_factory=lambda relay_url, target_url: page,
        )

        self.assertEqual(
            page.calls,
            [("simple", FIXED_ACTION_PROMPT, "assistant-1", 40.0)],
        )
        self.assertEqual(
            result,
            {"status": "uncertain", "reason": "causal_receipt_timeout"},
        )

    def test_need_input_marker_suppresses_fixed_action(self):
        page = FakePage(
            snapshot(
                assistant_text=(
                    "Please log in.\n[SUPERVISOR_STATE: NEED_INPUT]"
                )
            ),
            PromptDelivery(accepted=True),
        )

        result = run_once(
            TimerConfig(target_url=TARGET),
            page_factory=lambda relay_url, target_url: page,
        )

        self.assertEqual(result, {"status": "skipped", "reason": "need_input"})
        self.assertEqual(page.calls, [])

    def test_done_marker_suppresses_fixed_action(self):
        page = FakePage(
            snapshot(assistant_text="all complete\nSUPERVISOR_DONE"),
            PromptDelivery(accepted=True),
        )

        result = run_once(
            TimerConfig(target_url=TARGET),
            page_factory=lambda relay_url, target_url: page,
        )

        self.assertEqual(result, {"status": "skipped", "reason": "done"})
        self.assertEqual(page.calls, [])

    def test_need_input_wins_when_both_markers_are_present(self):
        page = FakePage(
            snapshot(
                assistant_text=(
                    "SUPERVISOR_DONE\n[SUPERVISOR_STATE: NEED_INPUT]"
                )
            ),
            PromptDelivery(accepted=True),
        )

        result = run_once(
            TimerConfig(target_url=TARGET),
            page_factory=lambda relay_url, target_url: page,
        )

        self.assertEqual(result, {"status": "skipped", "reason": "need_input"})
        self.assertEqual(page.calls, [])

    def test_human_gate_skips_without_attempting_delivery(self):
        page = FakePage(
            snapshot(user_turn_pending=True),
            PromptDelivery(accepted=True),
        )

        result = run_once(
            TimerConfig(target_url=TARGET),
            page_factory=lambda relay_url, target_url: page,
        )

        self.assertEqual(result, {"status": "skipped", "reason": "human_gate"})
        self.assertEqual(page.calls, [])
        self.assertTrue(page.closed)

    def test_run_once_skips_when_no_stable_assistant_turn_exists(self):
        page = FakePage(snapshot(turn_key=""), PromptDelivery(accepted=True))

        result = run_once(
            TimerConfig(target_url=TARGET),
            page_factory=lambda relay_url, target_url: page,
        )

        self.assertEqual(result, {"status": "skipped", "reason": "missing_assistant_turn"})
        self.assertEqual(page.calls, [])
        self.assertTrue(page.closed)

    def test_run_once_surfaces_uncertain_delivery_without_retry(self):
        page = FakePage(
            snapshot(),
            PromptDelivery(
                accepted=False,
                uncertain=True,
                reason="causal_receipt_timeout",
            ),
        )

        result = run_once(
            TimerConfig(target_url=TARGET),
            page_factory=lambda relay_url, target_url: page,
        )

        self.assertEqual(
            result,
            {"status": "uncertain", "reason": "causal_receipt_timeout"},
        )
        self.assertEqual(len(page.calls), 1)


if __name__ == "__main__":
    unittest.main()
