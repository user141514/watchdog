from __future__ import annotations

import unittest

from chat_watchdog.cli import _SupervisorWatcher
from chat_watchdog.model import PageSnapshot, Phase, is_need_input
from chat_watchdog.supervisor import CONTINUE_PROMPT, StepResult, Supervisor


class FakePage:
    target_url = "https://chatgpt.com/c/00000000-0000-0000-0000-000000000222"

    def __init__(self, snapshot: PageSnapshot) -> None:
        self.current = snapshot
        self.sent = 0

    def snapshot(self) -> PageSnapshot:
        return self.current

    def send_continue(self, prompt: str, expected_turn_key: tuple[int, str]) -> bool:
        self.sent += 1
        return True


class FakeAgentPool:
    candidate_names = ("recovery",)

    def __init__(self) -> None:
        self.calls = 0

    def try_acquire(self, prompt: str, exclude=()):
        self.calls += 1
        return None


class FakeAdmission:
    def __init__(self) -> None:
        self.calls = 0

    def admit(self, target_url: str):
        self.calls += 1
        return type("Result", (), {"admitted": True, "retry_after_ms": None})()


def finished(*, text: str, assistant_count: int = 1, user_count: int = 1, turn_id: str = "assistant-1") -> PageSnapshot:
    return PageSnapshot(
        phase=Phase.FINISHED,
        assistant_turn_id=turn_id,
        assistant_text_signature=f"sig-{turn_id}",
        assistant_text=text,
        assistant_count=assistant_count,
        user_count=user_count,
    )


class StubSupervisor:
    should_stop = False

    def __init__(self, result: StepResult) -> None:
        self.result = result

    def step(self) -> StepResult:
        return self.result

    def close(self) -> None:
        return None


class StubPage:
    target_url = "https://chatgpt.com/c/00000000-0000-0000-0000-000000000333"

    def snapshot(self) -> PageSnapshot:
        return finished(text="status")

    def close(self) -> None:
        return None


class HumanGateTests(unittest.TestCase):
    def test_need_input_marker_must_be_the_final_protocol_line(self) -> None:
        self.assertTrue(is_need_input("Please load the extension.\n[SUPERVISOR_STATE: NEED_INPUT]"))
        self.assertFalse(is_need_input("Example: [SUPERVISOR_STATE: NEED_INPUT] means pause."))
        self.assertFalse(is_need_input("[SUPERVISOR_STATE: NEED_INPUT]\nmore assistant text"))

    def test_continue_prompt_teaches_the_human_gate_protocol(self) -> None:
        self.assertIn("[SUPERVISOR_STATE: NEED_INPUT]", CONTINUE_PROMPT)

    def test_human_gate_suspends_without_send_recovery_or_admission(self) -> None:
        page = FakePage(finished(text="Please sign in.\n[SUPERVISOR_STATE: NEED_INPUT]"))
        agents = FakeAgentPool()
        admission = FakeAdmission()
        supervisor = Supervisor(page, agents, send_admission=admission)

        self.assertEqual(supervisor.step(), StepResult.NEED_INPUT)
        self.assertEqual(supervisor.step(), StepResult.NEED_INPUT)
        self.assertFalse(supervisor.should_stop)
        self.assertEqual(page.sent, 0)
        self.assertEqual(agents.calls, 0)
        self.assertEqual(admission.calls, 0)

    def test_conflicting_done_and_need_input_fails_closed_to_human_gate(self) -> None:
        page = FakePage(finished(text="SUPERVISOR_DONE\n[SUPERVISOR_STATE: NEED_INPUT]"))
        supervisor = Supervisor(page, FakeAgentPool(), send_admission=FakeAdmission())

        self.assertEqual(supervisor.step(), StepResult.NEED_INPUT)
        self.assertFalse(supervisor.should_stop)

    def test_registry_watcher_exposes_derived_human_gate_state(self) -> None:
        supervisor = StubSupervisor(StepResult.NEED_INPUT)
        watcher = _SupervisorWatcher(StubPage(), supervisor)

        watcher.step()
        self.assertEqual(watcher.state, "need_input")
        supervisor.result = StepResult.USER_TURN_PENDING
        watcher.step()
        self.assertEqual(watcher.state, "waiting")
        supervisor.result = StepResult.ACTIVE
        watcher.step()
        self.assertEqual(watcher.state, "active")

    def test_dom_interaction_gate_resumes_only_after_external_ui_state_changes(self) -> None:
        page = FakePage(PageSnapshot(
            phase=Phase.BLOCKED,
            assistant_turn_id="assistant-1",
            assistant_text_signature="sig-1",
            assistant_text="Waiting for approval",
            assistant_count=1,
            user_count=1,
            interaction_required=True,
        ))
        supervisor = Supervisor(page, FakeAgentPool())
        self.assertEqual(supervisor.step(), StepResult.NEED_INPUT)

        page.current = finished(text="SUPERVISOR_DONE")
        self.assertEqual(supervisor.step(), StepResult.DONE)
        self.assertTrue(supervisor.should_stop)

    def test_new_user_turn_waits_for_new_assistant_before_resuming(self) -> None:
        page = FakePage(finished(text="Please approve.\n[SUPERVISOR_STATE: NEED_INPUT]"))
        supervisor = Supervisor(page, FakeAgentPool(), send_admission=FakeAdmission())
        self.assertEqual(supervisor.step(), StepResult.NEED_INPUT)

        page.current = finished(
            text="Please approve.\n[SUPERVISOR_STATE: NEED_INPUT]",
            assistant_count=1,
            user_count=2,
            turn_id="assistant-1",
        )
        self.assertEqual(supervisor.step(), StepResult.USER_TURN_PENDING)
        self.assertEqual(page.sent, 0)

        page.current = PageSnapshot(
            phase=Phase.RESPONDING,
            assistant_turn_id="assistant-2",
            assistant_text_signature="sig-assistant-2",
            assistant_text="Continuing after the user action",
            assistant_count=1,
            user_count=2,
        )
        self.assertEqual(supervisor.step(), StepResult.ACTIVE)
        self.assertEqual(page.sent, 0)


if __name__ == "__main__":
    unittest.main()
