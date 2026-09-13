from __future__ import annotations

from enum import Enum
import time
from typing import Collection, Protocol

from .agent_runner import AgentLease
from .model import PageSnapshot, Phase, is_done
from .reanchor_bridge import ReanchorBridgeError, ReanchorOutcome


CONTINUE_PROMPT = (
    "继续当前任务，从已经完成的工作直接往下执行；不要重新调研、不要重复已经完成的步骤。"
    "如果整个任务已经真正完成，请在回复最后单独输出 SUPERVISOR_DONE。"
    "如果还没完成，就继续实际推进任务。"
)

RECOVERY_PROMPT = (
    "恢复当前已经打开的 ChatGPT 工作对话并推动它继续当前任务。不要新建对话，不要重做已完成步骤。"
    "如果只是当前 turn 已结束，触发下一 turn 继续；主 ChatGPT turn 恢复工作后保持退出等待被 watchdog 回收。"
)


class PagePort(Protocol):
    def snapshot(self) -> PageSnapshot: ...

    def send_continue(
        self,
        prompt: str,
        expected_turn_key: tuple[int, str],
    ) -> bool: ...


class ReanchorPort(Protocol):
    def record_frontend_fault(
        self,
        page: PagePort,
        snapshot: PageSnapshot,
    ) -> None: ...

    def step(
        self,
        page: PagePort,
        snapshot: PageSnapshot,
        *,
        reason: str = "resume",
    ) -> ReanchorOutcome: ...


class AgentPoolPort(Protocol):
    @property
    def candidate_names(self) -> tuple[str, ...]: ...

    def try_acquire(
        self,
        prompt: str,
        exclude: Collection[str] = (),
    ) -> AgentLease | None: ...


class StepResult(str, Enum):
    ACTIVE = "active"
    CONTINUED = "continued"
    ALREADY_HANDLED = "already_handled"
    RECOVERY_STARTED = "recovery_started"
    RECOVERY_RUNNING = "recovery_running"
    RECOVERY_UNAVAILABLE = "recovery_unavailable"
    RECOVERY_CLOSED = "recovery_closed"
    BLOCKED = "blocked"
    SEND_TIMEOUT = "send_timeout"
    STREAM_INTERRUPTED = "stream_interrupted"
    WAITING = "waiting"
    NEED_INPUT = "need_input"
    DELIVERY_UNCERTAIN = "delivery_uncertain"
    DONE = "done"


class Supervisor:
    def __init__(
        self,
        page: PagePort,
        agent_pool: AgentPoolPort,
        *,
        recovery_timeout_seconds: float = 120.0,
        heartbeat_seconds: float = 900.0,
        clock=time.monotonic,
        reanchor: ReanchorPort | None = None,
        send_admission=None,
    ) -> None:
        self._page = page
        self._agent_pool = agent_pool
        self._direct_attempted: set[tuple[int, str]] = set()
        self._continued: set[tuple[int, str]] = set()
        self.recovery_lease: AgentLease | None = None
        self._recovery_baseline: tuple[tuple[int, str], int] | None = None
        self._recovery_started_at: float | None = None
        self._failed_recovery_agents: set[str] = set()
        self._recovery_timeout_seconds = recovery_timeout_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._clock = clock
        self._reanchor = reanchor
        self._send_admission = send_admission
        self._last_progress_key: tuple[tuple[int, str], str, int] | None = None
        self._last_progress_at: float | None = None
        self._pending_reanchor_reason: str | None = None
        self.should_stop = False

    def _conversation_advanced(self, snapshot: PageSnapshot) -> bool:
        if self._recovery_baseline is None:
            return False
        turn_key, user_count = self._recovery_baseline
        return snapshot.turn_key != turn_key or snapshot.user_count > user_count

    def _close_recovery(self, *, reset_failures: bool) -> None:
        if self.recovery_lease is not None:
            self.recovery_lease.close()
        self.recovery_lease = None
        self._recovery_baseline = None
        self._recovery_started_at = None
        if reset_failures:
            self._failed_recovery_agents.clear()

    def _recovery_has_failed(self) -> bool:
        lease = self.recovery_lease
        if lease is None:
            return False
        if not lease.is_alive:
            return True
        if self._recovery_started_at is None:
            return False
        return (
            self._clock() - self._recovery_started_at
            >= self._recovery_timeout_seconds
        )

    def _drop_failed_recovery(self) -> None:
        if self.recovery_lease is None:
            return
        self._failed_recovery_agents.add(self.recovery_lease.agent_name)
        self._close_recovery(reset_failures=False)

    def _try_recovery(self, snapshot: PageSnapshot) -> StepResult:
        if self.recovery_lease is not None:
            return StepResult.RECOVERY_RUNNING
        target_url = getattr(self._page, "target_url", "")
        recovery_prompt = RECOVERY_PROMPT
        if target_url:
            recovery_prompt += (
                f"唯一允许操作的目标对话 URL 是 {target_url}。"
                "如果无法定位这个准确对话，退出而不是选择其他 ChatGPT 标签页。"
            )
        lease = self._agent_pool.try_acquire(
            recovery_prompt,
            exclude=self._failed_recovery_agents,
        )
        if lease is None:
            # One complete pass found no usable candidate. Clear the per-turn
            # exclusions so the next poll can retry agents that may recover.
            self._failed_recovery_agents.clear()
            return StepResult.RECOVERY_UNAVAILABLE
        self.recovery_lease = lease
        self._recovery_baseline = (snapshot.turn_key, snapshot.user_count)
        self._recovery_started_at = self._clock()
        return StepResult.RECOVERY_STARTED

    def _remember_frontend_fault(self, snapshot: PageSnapshot) -> None:
        if self._reanchor is None:
            return
        self._pending_reanchor_reason = "frontend_fault"
        try:
            self._reanchor.record_frontend_fault(self._page, snapshot)
        except ReanchorBridgeError:
            pass

    def _try_fault_recovery(self, snapshot: PageSnapshot) -> StepResult:
        if self.recovery_lease is not None:
            return StepResult.RECOVERY_RUNNING
        if self._send_admission is not None:
            target_url = getattr(self._page, "target_url", "")
            if not target_url:
                return StepResult.WAITING
            try:
                admission = self._send_admission.admit(target_url)
            except Exception:
                return StepResult.WAITING
            if getattr(admission, "admitted", False) is not True:
                return StepResult.WAITING
        return self._try_recovery(snapshot)

    def _observe_progress(self, snapshot: PageSnapshot) -> float:
        now = self._clock()
        progress_key = (
            snapshot.turn_key,
            snapshot.assistant_text_signature,
            snapshot.user_count,
        )
        if progress_key != self._last_progress_key:
            self._last_progress_key = progress_key
            self._last_progress_at = now
        elif self._last_progress_at is None:
            self._last_progress_at = now
        return now

    def _attempt_direct_continue(self, snapshot: PageSnapshot) -> StepResult | None:
        key = snapshot.turn_key
        if key in self._continued:
            return StepResult.ALREADY_HANDLED
        if key in self._direct_attempted:
            return StepResult.BLOCKED if self._send_admission is not None else None

        if self._send_admission is not None:
            target_url = getattr(self._page, "target_url", "")
            if not target_url:
                return StepResult.WAITING
            try:
                admission = self._send_admission.admit(target_url)
            except Exception:
                return StepResult.WAITING
            if getattr(admission, "admitted", False) is not True:
                return StepResult.WAITING

        self._direct_attempted.add(key)
        try:
            accepted = self._page.send_continue(CONTINUE_PROMPT, key)
        except Exception:
            accepted = False
        if not accepted:
            return StepResult.BLOCKED if self._send_admission is not None else None

        self._continued.add(key)
        return StepResult.CONTINUED

    def step(self) -> StepResult:
        snapshot = self._page.snapshot()
        now = self._observe_progress(snapshot)

        recovery_closed = False
        if self.recovery_lease is not None and self._conversation_advanced(snapshot):
            self._close_recovery(reset_failures=True)
            recovery_closed = True
        elif self._recovery_has_failed():
            self._drop_failed_recovery()

        if snapshot.phase in (Phase.THINKING, Phase.RESPONDING):
            if (
                self._reanchor is not None
                and self._last_progress_at is not None
                and now - self._last_progress_at >= self._heartbeat_seconds
                and self._pending_reanchor_reason is None
            ):
                self._pending_reanchor_reason = "heartbeat"
            return StepResult.RECOVERY_CLOSED if recovery_closed else StepResult.ACTIVE

        if snapshot.phase is Phase.BLOCKED:
            if snapshot.interaction_required:
                return StepResult.NEED_INPUT
            if snapshot.send_timeout:
                self._remember_frontend_fault(snapshot)
                return self._try_fault_recovery(snapshot)
            if snapshot.stream_interrupted:
                self._remember_frontend_fault(snapshot)
                return self._try_fault_recovery(snapshot)
            if (
                snapshot.assistant_text.strip()
                and self._last_progress_at is not None
                and now - self._last_progress_at >= self._recovery_timeout_seconds
            ):
                direct = self._attempt_direct_continue(snapshot)
                if direct is not None:
                    return direct
            return StepResult.BLOCKED

        if self._reanchor is not None:
            reason = self._pending_reanchor_reason or "resume"
            try:
                outcome = self._reanchor.step(
                    self._page,
                    snapshot,
                    reason=reason,
                )
            except ReanchorBridgeError:
                return StepResult.BLOCKED
            self._pending_reanchor_reason = None
            if outcome is ReanchorOutcome.DONE:
                self._close_recovery(reset_failures=True)
                self.should_stop = True
                return StepResult.DONE
            if outcome is ReanchorOutcome.CONTINUED:
                return StepResult.CONTINUED
            if outcome is ReanchorOutcome.WAITING:
                return StepResult.WAITING
            if outcome is ReanchorOutcome.NEED_INPUT:
                return StepResult.NEED_INPUT
            if outcome is ReanchorOutcome.DELIVERY_UNCERTAIN:
                return StepResult.DELIVERY_UNCERTAIN
            return StepResult.BLOCKED

        if is_done(snapshot.assistant_text):
            self._close_recovery(reset_failures=True)
            self.should_stop = True
            return StepResult.DONE

        direct = self._attempt_direct_continue(snapshot)
        if direct is not None:
            return direct

        return self._try_recovery(snapshot)

    def close(self) -> None:
        self._close_recovery(reset_failures=True)

    def run(self, poll_interval: float = 60.0) -> None:
        try:
            while not self.should_stop:
                self.step()
                if not self.should_stop:
                    time.sleep(poll_interval)
        finally:
            self.close()
