from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import time
from typing import Collection, Protocol

from .agent_runner import AgentLease
from .model import PageSnapshot, Phase, PromptDelivery, TurnKey, is_done, is_need_input
from .reanchor_bridge import ReanchorBridgeError, ReanchorOutcome
from .state_client import StateProtocolError, StateUnavailable


CONTINUE_PROMPT = (
    "继续当前任务，从已经完成的工作直接往下执行；不要重新调研、不要重复已经完成的步骤。"
    "如果整个任务已经真正完成，请在回复最后单独输出 SUPERVISOR_DONE。"
    "如果继续需要用户手动操作、登录、授权、确认或补充信息，请说明需要的动作，"
    "并在回复最后单独输出 [SUPERVISOR_STATE: NEED_INPUT]；不要自行假定用户已经完成。"
    "如果还没完成且不需要用户介入，就继续实际推进任务。"
)

RECOVERY_PROMPT = (
    "恢复当前已经打开的 ChatGPT 工作对话并推动它继续当前任务。不要新建对话，不要重做已完成步骤。"
    "如果只是当前 turn 已结束，触发下一 turn 继续；主 ChatGPT turn 恢复工作后保持退出等待被 watchdog 回收。"
)

LIVENESS_TIMEOUT_SECONDS = 360.0


class PagePort(Protocol):
    def snapshot(self) -> PageSnapshot: ...

    def send_continue(
        self,
        prompt: str,
        expected_turn_key: TurnKey,
    ) -> bool | PromptDelivery: ...

    def retry_fault(self, expected_turn_key: TurnKey) -> bool | PromptDelivery: ...


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


class ProgressStorePort(Protocol):
    def observe(
        self,
        conversation_id: str,
        target_url: str,
        fingerprint: str | None,
        *,
        observable: bool,
    ): ...

    def claim_stall(
        self,
        conversation_id: str,
        *,
        timeout_seconds: float,
        state_version: int,
        writer_epoch: int,
    ) -> bool: ...


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
    USER_TURN_PENDING = "user_turn_pending"
    DELIVERY_UNCERTAIN = "delivery_uncertain"
    DONE = "done"


@dataclass(frozen=True)
class _AuthoritativeIntentSnapshot:
    user_turn_id: str
    assistant_turn_id: str

    @property
    def turn_key(self) -> TurnKey:
        return self.assistant_turn_id


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
        intent_client=None,
        state_client=None,
        progress_store: ProgressStorePort | None = None,
        progress_id: str | None = None,
        active_stall_seconds: float = 300.0,
    ) -> None:
        self._page = page
        self._agent_pool = agent_pool
        self._direct_attempted: set[TurnKey] = set()
        self._continued: set[TurnKey] = set()
        self.recovery_lease: AgentLease | None = None
        self._recovery_baseline: tuple[int, str] | None = None
        self._recovery_started_at: float | None = None
        self._failed_recovery_agents: set[str] = set()
        self._recovery_timeout_seconds = recovery_timeout_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._clock = clock
        self._reanchor = reanchor
        self._send_admission = send_admission
        if intent_client is not None and reanchor is not None:
            raise ValueError('managed intents cannot share a direct-write reanchor path')
        if state_client is not None and intent_client is None:
            raise ValueError('authoritative state requires managed intent ownership')
        if progress_store is not None and (state_client is None or intent_client is None):
            raise ValueError('mymem_lite progress requires authoritative managed mode')
        if progress_store is not None and not progress_id:
            raise ValueError('mymem_lite progress requires a conversation id')
        if active_stall_seconds <= 0:
            raise ValueError('active_stall_seconds must be positive')
        self._intent_client = intent_client
        self._state_client = state_client
        self._progress_store = progress_store
        self._progress_id = progress_id
        self._active_stall_seconds = active_stall_seconds
        self._last_progress_key: tuple[TurnKey, str] | None = None
        self._last_progress_at: float | None = None
        self._liveness_attempted_for: tuple[TurnKey, str] | None = None
        self._pending_reanchor_reason: str | None = None
        self._human_gate: tuple[int, str, str] | None = None
        self.should_stop = False
        self.diagnostics: dict[str, object] = {}

    def _conversation_advanced(self, snapshot: PageSnapshot) -> bool:
        if self._recovery_baseline is None:
            return False
        baseline_receipt_seq, _baseline_assistant_id = self._recovery_baseline
        # A causal submission receipt is created only from a send UI event that
        # occurred after the baseline and was then bound to a newly mounted
        # stable user message. Mounted-tail ID inequality alone is never order.
        return bool(
            snapshot.submission_receipt_seq > baseline_receipt_seq
            and snapshot.submission_receipt_id
        )

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
        if self._intent_client is not None:
            return StepResult.BLOCKED
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
        self._recovery_baseline = (
            snapshot.submission_receipt_seq,
            snapshot.assistant_turn_id,
        )
        self._recovery_started_at = self._clock()
        return StepResult.RECOVERY_STARTED

    @staticmethod
    def _delivery_status(value: bool | PromptDelivery) -> str:
        if isinstance(value, PromptDelivery):
            if value.uncertain:
                return "uncertain"
            if value.stale:
                return "stale"
            return "accepted" if value.accepted else "rejected"
        return "accepted" if value else "rejected"

    def _remember_frontend_fault(self, snapshot: PageSnapshot) -> None:
        if self._reanchor is None:
            return
        self._pending_reanchor_reason = "frontend_fault"
        try:
            self._reanchor.record_frontend_fault(self._page, snapshot)
        except ReanchorBridgeError:
            pass

    def _try_fault_recovery(self, snapshot: PageSnapshot) -> StepResult:
        if self._intent_client is not None:
            return self._publish_intent(snapshot, kind='retry')
        if self.recovery_lease is not None:
            return StepResult.RECOVERY_RUNNING
        if snapshot.turn_key in self._continued:
            return StepResult.ALREADY_HANDLED
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

        retry_fault = getattr(self._page, "retry_fault", None)
        if callable(retry_fault):
            try:
                delivery = retry_fault(snapshot.turn_key)
            except Exception:
                delivery = False
            status = self._delivery_status(delivery)
            if status == "accepted":
                self._continued.add(snapshot.turn_key)
                return StepResult.CONTINUED
            if status == "uncertain":
                return StepResult.DELIVERY_UNCERTAIN
            if status == "stale":
                return StepResult.WAITING
            if self._send_admission is not None:
                return StepResult.BLOCKED
        elif self._send_admission is not None:
            return StepResult.BLOCKED

        return self._try_recovery(snapshot)

    def _suspend_for_human(self, snapshot: PageSnapshot) -> None:
        self._close_recovery(reset_failures=True)
        self._human_gate = (
            snapshot.trusted_submission_receipt_seq,
            snapshot.assistant_turn_id.strip(),
            "",
        )

    def _has_authoritative_identity(self, snapshot: PageSnapshot) -> bool:
        target_url = str(getattr(self._page, "target_url", "") or "")
        if "/c/" not in target_url:
            return False
        assistant_turn_id = snapshot.assistant_turn_id.strip()
        if not assistant_turn_id:
            return False
        return assistant_turn_id not in {
            "assistant-0",
            "invalid-snapshot",
            "navigated-away",
            "relay-unavailable",
        }

    def _verified_progress_key(
        self,
        snapshot: PageSnapshot,
    ) -> tuple[TurnKey, str] | None:
        if snapshot.send_timeout or snapshot.stream_interrupted:
            return None
        if not self._has_authoritative_identity(snapshot):
            return None
        return (
            snapshot.turn_key,
            snapshot.assistant_text_signature,
        )

    def _same_liveness_assistant_state(self, snapshot: PageSnapshot) -> bool:
        if self._liveness_attempted_for is None:
            return False
        attempted_turn_key, attempted_signature = self._liveness_attempted_for
        return (
            snapshot.turn_key == attempted_turn_key
            and snapshot.assistant_text_signature == attempted_signature
        )

    def _observe_progress(self, snapshot: PageSnapshot) -> float:
        now = self._clock()
        progress_key = self._verified_progress_key(snapshot)
        if progress_key is None:
            # Liveness requires one continuous observable identity window. An
            # identity/fault gap invalidates the previous deadline rather than
            # letting an old timestamp survive across unobservable progress.
            self._last_progress_key = None
            self._last_progress_at = None
            return now
        if progress_key != self._last_progress_key:
            if self._same_liveness_assistant_state(snapshot):
                return now
            self._last_progress_key = progress_key
            self._last_progress_at = now
            self._liveness_attempted_for = None
        return now

    def _liveness_due(self, snapshot: PageSnapshot, *, now: float) -> bool:
        progress_key = self._verified_progress_key(snapshot)
        return bool(
            progress_key is not None
            and not snapshot.user_turn_pending
            and snapshot.assistant_text.strip()
            and progress_key == self._last_progress_key
            and self._last_progress_at is not None
            and now - self._last_progress_at >= LIVENESS_TIMEOUT_SECONDS
            and self._liveness_attempted_for != progress_key
        )

    def _try_liveness_continue(
        self,
        snapshot: PageSnapshot,
        *,
        now: float,
    ) -> StepResult:
        # Reanchor and authoritative Sidecar modes own their delivery path. A
        # time threshold is never permission to create a second writer.
        if self._reanchor is not None:
            return StepResult.BLOCKED

        progress_key = self._verified_progress_key(snapshot)
        if not self._liveness_due(snapshot, now=now):
            return StepResult.BLOCKED
        assert progress_key is not None

        # Admission/intent ownership still applies. Consume the liveness epoch
        # before any potentially ambiguous delivery so a lost ACK cannot cause
        # a timeout resend on the same assistant state.
        self._liveness_attempted_for = progress_key
        if self._intent_client is not None:
            return self._publish_intent(snapshot)

        key = snapshot.turn_key
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
            sender = getattr(
                self._page,
                "send_liveness_continue",
                self._page.send_continue,
            )
            delivery = sender(CONTINUE_PROMPT, key)
        except Exception:
            delivery = False
        status = self._delivery_status(delivery)
        if status == "accepted":
            self._continued.add(key)
            return StepResult.CONTINUED
        if status == "uncertain":
            return StepResult.DELIVERY_UNCERTAIN
        if status == "stale":
            return StepResult.WAITING
        return StepResult.BLOCKED

    def _publish_intent(self, snapshot, *, kind: str = 'continue', authoritative_state=None) -> StepResult:
        if snapshot.turn_key in self._continued:
            return StepResult.ALREADY_HANDLED
        try:
            if authoritative_state is not None:
                result = self._intent_client.submit_v1(authoritative_state, CONTINUE_PROMPT)
            else:
                result = self._intent_client.submit(self._page.target_url, snapshot, CONTINUE_PROMPT, kind=kind)
        except Exception as error:
            self.diagnostics.update(intent_accepted=None, intent_reason='delivery_uncertain',
                                    error=f'{type(error).__name__}: {error}'[:1000])
            return StepResult.DELIVERY_UNCERTAIN
        self.diagnostics.update(intent_accepted=result.get('accepted'), intent_reason=result.get('reason'))
        if result.get('accepted') is True:
            self._continued.add(snapshot.turn_key)
            return StepResult.CONTINUED
        reason = result.get('reason')
        if reason == 'delivery_uncertain':
            return StepResult.DELIVERY_UNCERTAIN
        if reason == 'need_input':
            if authoritative_state is None:
                self._suspend_for_human(snapshot)
            return StepResult.NEED_INPUT
        if authoritative_state is not None and reason in {
            'stale_state', 'stale_intent', 'writer_epoch_mismatch',
            'state_delivery_uncertain', 'state_not_continuable',
            'pacing', 'busy', 'user_turn_pending', 'target_unavailable'
        }:
            return StepResult.WAITING
        if authoritative_state is not None and reason == 'writer_mode_mismatch':
            return StepResult.BLOCKED
        if reason == 'stale_intent':
            self._continued.add(snapshot.turn_key)
            return StepResult.ALREADY_HANDLED
        if reason in {'pacing', 'busy', 'user_turn_pending'}:
            return StepResult.WAITING
        return StepResult.BLOCKED

    def _authoritative_identity(self, state: dict) -> _AuthoritativeIntentSnapshot | None:
        turn = state.get('turn') or {}
        user_id = turn.get('userMessageId')
        assistant_id = turn.get('assistantMessageId')
        if not isinstance(user_id, str) or not user_id or not isinstance(assistant_id, str) or not assistant_id:
            return None
        return _AuthoritativeIntentSnapshot(user_id, assistant_id)

    @staticmethod
    def _snapshot_available(snapshot: PageSnapshot | None) -> bool:
        return bool(
            snapshot is not None
            and snapshot.assistant_turn_id not in {
                'relay-unavailable', 'invalid-snapshot', 'navigated-away'
            }
        )

    def _observe_managed_progress(self, state: dict, snapshot: PageSnapshot | None) -> bool:
        if self._progress_store is None or self._progress_id is None:
            return False
        turn = state.get('turn') or {}
        user_id = turn.get('userMessageId')
        assistant_id = turn.get('assistantMessageId')
        observable = bool(
            self._snapshot_available(snapshot)
            and isinstance(user_id, str)
            and user_id
            and snapshot is not None
            and snapshot.user_turn_id == user_id
            and (not assistant_id or snapshot.assistant_turn_id == assistant_id)
        )
        fingerprint = None
        if observable and snapshot is not None:
            fingerprint = json.dumps([
                state.get('stateVersion'),
                state.get('progress'),
                state.get('body'),
                state.get('delivery'),
                state.get('gate'),
                turn.get('turnId'),
                user_id,
                assistant_id,
                snapshot.phase.value,
                snapshot.assistant_turn_id,
                snapshot.assistant_text_signature,
                snapshot.user_turn_pending,
            ], ensure_ascii=False, separators=(',', ':'))
        try:
            pulse = self._progress_store.observe(
                self._progress_id,
                self._page.target_url,
                fingerprint,
                observable=observable,
            )
        except Exception as error:
            self.diagnostics.update(
                progress_observable=False,
                progress_error=f'{type(error).__name__}: {error}'[:1000],
            )
            return False
        self.diagnostics.update(
            progress_observable=observable,
            progress_seq=getattr(pulse, 'progress_seq', None),
            progress_last_at=getattr(pulse, 'last_progress_at', None),
            progress_file_bytes=getattr(pulse, 'file_bytes', None),
        )
        return observable

    def _publish_stop(self, authoritative_state) -> StepResult:
        try:
            result = self._intent_client.submit_v1(
                authoritative_state,
                None,
                action='stop',
            )
        except Exception as error:
            self.diagnostics.update(
                intent_accepted=None,
                intent_reason='delivery_uncertain',
                error=f'{type(error).__name__}: {error}'[:1000],
            )
            return StepResult.DELIVERY_UNCERTAIN
        self.diagnostics.update(
            intent_accepted=result.get('accepted'),
            intent_reason=result.get('reason'),
            recovery_action='stop',
        )
        if result.get('accepted') is True:
            return StepResult.RECOVERY_STARTED
        reason = result.get('reason')
        if reason == 'delivery_uncertain':
            return StepResult.DELIVERY_UNCERTAIN
        if reason == 'need_input':
            return StepResult.NEED_INPUT
        if reason in {
            'stale_state', 'stale_intent', 'writer_epoch_mismatch',
            'state_delivery_uncertain', 'state_not_stoppable',
            'busy', 'user_turn_pending', 'target_unavailable',
        }:
            return StepResult.WAITING
        if reason == 'writer_mode_mismatch':
            return StepResult.BLOCKED
        return StepResult.BLOCKED

    def _step_authoritative(self, snapshot: PageSnapshot | None) -> StepResult:
        self.diagnostics = {
            'observed_at': time.time(),
            'snapshot_available': self._snapshot_available(snapshot),
            'state_available': False, 'reason': None, 'error': None,
        }
        try:
            value = self._state_client.read(self._page.target_url)
        except StateUnavailable as error:
            self.diagnostics.update(reason='state_owner_unavailable', error=str(error)[:1000])
            return StepResult.WAITING
        except StateProtocolError as error:
            self.diagnostics.update(reason='state_protocol_error', error=str(error)[:1000])
            return StepResult.BLOCKED
        except Exception as error:
            self.diagnostics.update(reason='state_read_error', error=str(error)[:1000])
            return StepResult.BLOCKED

        state = value.to_dict()
        writer = state.get('writer') or {}
        self.diagnostics.update(
            state_available=True, state_version=state.get('stateVersion'),
            progress=state.get('progress'), body=state.get('body'),
            delivery=state.get('delivery'), gate=state.get('gate'),
            writer_mode=writer.get('mode'), writer_epoch=writer.get('epoch'),
        )
        progress_observable = self._observe_managed_progress(state, snapshot)
        if writer.get('mode') != 'managed':
            return StepResult.BLOCKED

        delivery = state.get('delivery')
        if delivery == 'uncertain':
            return StepResult.DELIVERY_UNCERTAIN

        if state.get('gate') == 'human_required':
            return StepResult.NEED_INPUT

        progress = state.get('progress')
        body = state.get('body')
        if progress == 'active':
            if (
                self._progress_store is not None
                and self._progress_id is not None
                and progress_observable
                and delivery == 'delivered'
            ):
                try:
                    stalled = self._progress_store.claim_stall(
                        self._progress_id,
                        timeout_seconds=self._active_stall_seconds,
                        state_version=int(state.get('stateVersion', 0)),
                        writer_epoch=int(writer.get('epoch', 0)),
                    )
                except Exception as error:
                    self.diagnostics.update(
                        progress_error=f'{type(error).__name__}: {error}'[:1000]
                    )
                    stalled = False
                self.diagnostics['active_stall_claimed'] = stalled
                if stalled:
                    return self._publish_stop(value)
            return StepResult.ACTIVE
        if progress in {'unknown', 'idle'}:
            return StepResult.WAITING
        if delivery != 'delivered':
            return StepResult.WAITING

        authoritative = self._authoritative_identity(state)
        if authoritative is None:
            return StepResult.WAITING

        if progress == 'blocked':
            if body in {'empty', 'incomplete'}:
                return self._publish_intent(authoritative, authoritative_state=value)
            return StepResult.WAITING

        if progress == 'terminal':
            turn = state.get('turn') or {}
            if snapshot is None or (
                snapshot.user_turn_id != turn.get('userMessageId')
                or snapshot.assistant_turn_id != turn.get('assistantMessageId')
            ):
                return StepResult.WAITING
            if is_done(snapshot.assistant_text):
                self._close_recovery(reset_failures=True)
                self.should_stop = True
                return StepResult.DONE
            return self._publish_intent(authoritative, authoritative_state=value)

        return StepResult.WAITING

    def _attempt_direct_continue(self, snapshot: PageSnapshot) -> StepResult | None:
        if self.recovery_lease is not None:
            return StepResult.RECOVERY_RUNNING
        if self._intent_client is not None:
            return self._publish_intent(snapshot)
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
            delivery = self._page.send_continue(CONTINUE_PROMPT, key)
        except Exception:
            delivery = False
        status = self._delivery_status(delivery)
        if status == "uncertain":
            return StepResult.DELIVERY_UNCERTAIN
        if status == "stale":
            return StepResult.WAITING
        if status != "accepted":
            self._direct_attempted.discard(key)
            return StepResult.BLOCKED if self._send_admission is not None else None

        self._continued.add(key)
        return StepResult.CONTINUED

    def step(self) -> StepResult:
        if self._state_client is not None and self._intent_client is not None:
            try:
                snapshot = self._page.snapshot()
            except Exception:
                snapshot = None
            result = self._step_authoritative(snapshot)
            self.diagnostics['decision'] = result.value
            return result
        snapshot = self._page.snapshot()
        now = self._observe_progress(snapshot)

        if self._human_gate is not None:
            baseline_trusted_seq, gate_assistant_turn_id, expected_user_turn_id = self._human_gate
            if not expected_user_turn_id:
                if (
                    snapshot.trusted_submission_receipt_seq <= baseline_trusted_seq
                    or not snapshot.trusted_submission_receipt_id
                ):
                    return StepResult.NEED_INPUT
                expected_user_turn_id = snapshot.trusted_submission_receipt_id
                self._human_gate = (
                    baseline_trusted_seq,
                    gate_assistant_turn_id,
                    expected_user_turn_id,
                )

            # The trusted receipt proves the human submitted after the gate;
            # wait until the mounted conversation catches up to that exact user
            # message and a new assistant turn exists before releasing policy.
            if snapshot.user_turn_id != expected_user_turn_id:
                return StepResult.USER_TURN_PENDING
            if snapshot.user_turn_pending:
                return StepResult.USER_TURN_PENDING
            current_assistant_turn_id = snapshot.assistant_turn_id.strip()
            if (
                not current_assistant_turn_id
                or current_assistant_turn_id == gate_assistant_turn_id
            ):
                return StepResult.USER_TURN_PENDING
            self._human_gate = None

        recovery_closed = False
        if self.recovery_lease is not None and self._conversation_advanced(snapshot):
            self._close_recovery(reset_failures=True)
            recovery_closed = True
        elif self._recovery_has_failed():
            self._drop_failed_recovery()

        if snapshot.phase in (Phase.THINKING, Phase.RESPONDING):
            # Human-gate protocol markers preempt liveness recovery even if the
            # frontend still reports an active generation phase. This is a
            # fail-closed guard against stale/broken UI generation state after
            # the assistant has already emitted its terminal NEED_INPUT line.
            if is_need_input(snapshot.assistant_text):
                self._suspend_for_human(snapshot)
                return StepResult.NEED_INPUT
            if (
                self._reanchor is None
                and self._liveness_due(snapshot, now=now)
            ):
                return self._try_liveness_continue(snapshot, now=now)
            if (
                self._reanchor is not None
                and self._last_progress_at is not None
                and now - self._last_progress_at >= self._heartbeat_seconds
                and self._pending_reanchor_reason is None
            ):
                self._pending_reanchor_reason = "heartbeat"
            return StepResult.RECOVERY_CLOSED if recovery_closed else StepResult.ACTIVE

        if is_need_input(snapshot.assistant_text):
            self._suspend_for_human(snapshot)
            return StepResult.NEED_INPUT

        if self._reanchor is None and is_done(snapshot.assistant_text):
            self._close_recovery(reset_failures=True)
            self.should_stop = True
            return StepResult.DONE

        if snapshot.phase is Phase.BLOCKED:
            if snapshot.interaction_required:
                return StepResult.NEED_INPUT
            if snapshot.send_timeout:
                self._remember_frontend_fault(snapshot)
                return self._try_fault_recovery(snapshot)
            if snapshot.stream_interrupted:
                self._remember_frontend_fault(snapshot)
                return self._try_fault_recovery(snapshot)
            # A BLOCKED turn has already stopped; preserve the upstream
            # configurable recovery timeout and retry semantics. The fixed
            # 360-second one-shot liveness path is only for a frontend that
            # still reports THINKING/RESPONDING while visible output is stale.
            if (
                self._has_authoritative_identity(snapshot)
                and snapshot.assistant_text.strip()
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
                self._suspend_for_human(snapshot)
                return StepResult.NEED_INPUT
            if outcome is ReanchorOutcome.DELIVERY_UNCERTAIN:
                return StepResult.DELIVERY_UNCERTAIN
            return StepResult.BLOCKED

        if is_done(snapshot.assistant_text):
            self._close_recovery(reset_failures=True)
            self.should_stop = True
            return StepResult.DONE

        if self._same_liveness_assistant_state(snapshot):
            return StepResult.ALREADY_HANDLED

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
