from __future__ import annotations

import unittest

from chat_watchdog.model import PageSnapshot, Phase
from chat_watchdog.supervisor import StepResult, Supervisor


class FakePage:
    target_url = "https://chatgpt.com/c/00000000-0000-0000-0000-000000000111"

    def __init__(
        self,
        *,
        send_result: bool = True,
        send_timeout: bool = False,
        stream_interrupted: bool = False,
        retry_result: bool = True,
    ) -> None:
        self.send_result = send_result
        self.send_timeout = send_timeout
        self.stream_interrupted = stream_interrupted
        self.retry_result = retry_result
        self.sent = 0
        self.retried = 0

    def snapshot(self) -> PageSnapshot:
        faulted = self.send_timeout or self.stream_interrupted
        return PageSnapshot(
            phase=Phase.BLOCKED if faulted else Phase.FINISHED,
            assistant_turn_id="assistant-1",
            assistant_text_signature="sig",
            assistant_text="phase one",
            assistant_count=1,
            user_count=1,
            send_timeout=self.send_timeout,
            stream_interrupted=self.stream_interrupted,
            fault_text="frontend delivery fault" if faulted else "",
        )

    def send_continue(self, prompt: str, expected_turn_key: tuple[int, str]) -> bool:
        self.sent += 1
        return self.send_result

    def retry_fault(self, expected_turn_key: tuple[int, str]) -> bool:
        self.retried += 1
        return self.retry_result


class FakeAgentPool:
    candidate_names = ("recovery",)

    def __init__(self) -> None:
        self.calls = 0

    def try_acquire(self, prompt: str, exclude=()):
        self.calls += 1
        return None


class Admission:
    def __init__(self, *, admitted: bool = True, fail: bool = False) -> None:
        self.admitted = admitted
        self.fail = fail
        self.calls: list[str] = []

    def admit(self, target_url: str):
        self.calls.append(target_url)
        if self.fail:
            raise RuntimeError("sidecar admission unavailable")
        return type("Result", (), {"admitted": self.admitted, "retry_after_ms": 12_000})()


class SupervisorAdmissionTests(unittest.TestCase):
    def test_managed_continue_requires_admission_before_dom_send(self) -> None:
        page = FakePage()
        agents = FakeAgentPool()
        admission = Admission(admitted=True)
        supervisor = Supervisor(page, agents, send_admission=admission)

        self.assertEqual(supervisor.step(), StepResult.CONTINUED)
        self.assertEqual(admission.calls, [page.target_url])
        self.assertEqual(page.sent, 1)
        self.assertEqual(agents.calls, 0)

    def test_admission_denial_waits_without_sending_or_starting_recovery(self) -> None:
        page = FakePage()
        agents = FakeAgentPool()
        admission = Admission(admitted=False)
        supervisor = Supervisor(page, agents, send_admission=admission)

        self.assertEqual(supervisor.step(), StepResult.WAITING)
        self.assertEqual(page.sent, 0)
        self.assertEqual(agents.calls, 0)

    def test_admission_transport_failure_is_fail_closed(self) -> None:
        page = FakePage()
        agents = FakeAgentPool()
        admission = Admission(fail=True)
        supervisor = Supervisor(page, agents, send_admission=admission)

        self.assertEqual(supervisor.step(), StepResult.WAITING)
        self.assertEqual(page.sent, 0)
        self.assertEqual(agents.calls, 0)

    def test_failed_dom_send_after_managed_grant_does_not_fall_through_to_recovery(self) -> None:
        page = FakePage(send_result=False)
        agents = FakeAgentPool()
        supervisor = Supervisor(page, agents, send_admission=Admission(admitted=True))

        self.assertEqual(supervisor.step(), StepResult.BLOCKED)
        self.assertEqual(supervisor.step(), StepResult.BLOCKED)
        self.assertEqual(page.sent, 1)
        self.assertEqual(agents.calls, 0)

    def test_legacy_unconfigured_mode_keeps_existing_recovery_fallback(self) -> None:
        page = FakePage(send_result=False)
        agents = FakeAgentPool()
        supervisor = Supervisor(page, agents)

        self.assertEqual(supervisor.step(), StepResult.RECOVERY_UNAVAILABLE)
        self.assertEqual(page.sent, 1)
        self.assertEqual(agents.calls, 1)

    def test_send_timeout_retries_exact_page_after_shared_admission(self) -> None:
        page = FakePage(send_timeout=True)
        agents = FakeAgentPool()
        admission = Admission(admitted=True)
        supervisor = Supervisor(page, agents, send_admission=admission)

        self.assertEqual(supervisor.step(), StepResult.CONTINUED)
        self.assertEqual(admission.calls, [page.target_url])
        self.assertEqual(page.retried, 1)
        self.assertEqual(page.sent, 0)
        self.assertEqual(agents.calls, 0)

    def test_send_timeout_waits_when_shared_admission_is_denied(self) -> None:
        page = FakePage(send_timeout=True)
        agents = FakeAgentPool()
        admission = Admission(admitted=False)
        supervisor = Supervisor(page, agents, send_admission=admission)

        self.assertEqual(supervisor.step(), StepResult.WAITING)
        self.assertEqual(page.sent, 0)
        self.assertEqual(agents.calls, 0)

    def test_managed_fault_retry_failure_stays_exact_target_and_never_spawns_agent(self) -> None:
        page = FakePage(send_timeout=True, retry_result=False)
        agents = FakeAgentPool()
        supervisor = Supervisor(page, agents, send_admission=Admission(admitted=True))

        self.assertEqual(supervisor.step(), StepResult.BLOCKED)
        self.assertEqual(page.retried, 1)
        self.assertEqual(page.sent, 0)
        self.assertEqual(agents.calls, 0)

    def test_stream_interrupted_retries_exact_page_in_legacy_mode(self) -> None:
        page = FakePage(stream_interrupted=True)
        agents = FakeAgentPool()
        supervisor = Supervisor(page, agents)

        self.assertEqual(supervisor.step(), StepResult.CONTINUED)
        self.assertEqual(page.retried, 1)
        self.assertEqual(page.sent, 0)
        self.assertEqual(agents.calls, 0)


if __name__ == "__main__":
    unittest.main()
