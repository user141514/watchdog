from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
import json
import logging
import math
from numbers import Real
import time
from typing import Callable

from .model import PageSnapshot, Phase, is_need_input
from .intent_client import SidecarIntentClient
from .observation_client import (
    ObservationProtocolError, ObservationUnavailable,
    SidecarObservationClient, SidecarObservationPage,
)
from .registry import conversation_id_from_url
from .relay_cdp import RelayCdpProtocol
from .relay_page import (
    RelayChatGPTPage,
    RelayTargetSelectionError,
    discover_websocket_url,
)


_LOG = logging.getLogger(__name__)

RENDER_WAIT_SECONDS = 60.0
SUBMISSION_CONFIRM_SECONDS = 90.0
PROGRESS_POLL_SECONDS = 10.0
PROGRESS_WINDOW_SECONDS = 90.0
DEFAULT_SIMPLE_INTERVAL_SECONDS = 15.0
SIMPLE_INACTIVITY_TIMEOUT_SECONDS = 15 * 60.0
RUNNING_STATUS = "RUNNING"
DONE_STATUS = "DONE"

ACTION_RECOVERY_PROMPT = (
    "继续当前 ACTION，从已经完成的内容直接往下推进；不要重新调研、不要重复已完成步骤。"
    "你仍处于 ACTION phase=0：不得输出 SUPERVISOR_DONE，也不得自行终止整个循环。"
    "如果继续确实需要用户手动操作、登录、授权、确认或补充信息，"
    "可以在回复最后单独输出 [SUPERVISOR_STATE: NEED_INPUT]。"
)

REVIEW_RECOVERY_PROMPT = (
    "继续完成当前 REVIEW；只审查紧邻的上一轮 ACTION，不执行 ACTION，不使用工具。"
    "你仍处于 REVIEW phase=1。严格遵守当前 REVIEW 协议，最终只输出一个 JSON 对象："
    "未完成则 decision=CONTINUE 并给出非空 next_prompt；"
    "只有总体目标已经充分完成时才允许 decision=DONE 且 terminal=SUPERVISOR_DONE。"
    "不要 markdown，不要在 JSON 外输出任何文字。"
)

ACTION_FIXED_PROMPT = "这是 15 分钟固定兜底。保持当前 ACTION 合约。" + ACTION_RECOVERY_PROMPT
REVIEW_FIXED_PROMPT = "这是 15 分钟固定兜底。保持当前 REVIEW 合约。" + REVIEW_RECOVERY_PROMPT

REVIEW_PROMPT = (
    "REVIEW {cycle}。只回顾紧邻的上一轮 ACTION，不继续执行任务，不使用工具。\n"
    "机械协议：你是 REVIEW，相位固定为 1；只有 REVIEW 有资格终止整个循环。"
    "上一轮 ACTION 即使输出过 SUPERVISOR_DONE，也没有终止权限。\n"
    "如果总体目标尚未完成，只生成下一轮唯一 ACTION prompt；它必须承接上一轮真实产出、"
    "禁止重复已完成工作，并明确 ACTION 不得输出 SUPERVISOR_DONE。\n"
    "未完成时严格只输出一个 JSON 对象："
    '{{"decision":"CONTINUE","review":"<上一ACTION完成了什么、还缺什么>",'
    '"next_prompt":"<下一ACTION完整提示词>"}}\n'
    "只有当总体目标已经充分完成时，才允许终止，并严格只输出："
    '{{"decision":"DONE","review":"<为什么总体目标已完成>",'
    '"terminal":"SUPERVISOR_DONE"}}\n'
    "不要 markdown，不要在 JSON 外输出任何文字。"
)


def _positive_fixed_time(value: object, label: str) -> float:
    if (isinstance(value, bool) or not isinstance(value, Real)
            or not math.isfinite(value) or value <= 0):
        raise ValueError(f"{label} must be a finite positive real number")
    return float(value)


def _same_conversation(left: str, right: str) -> bool:
    try:
        return conversation_id_from_url(left) == conversation_id_from_url(right)
    except ValueError:
        return False


def _terminal_status(text: str) -> str | None:
    tail = (text or "")[-4096:]
    if is_need_input(tail):
        return "need_input"
    return None


def _parse_review(text: str) -> tuple[str, str | None]:
    raw = (text or "").strip()
    if raw.startswith("```json") and raw.endswith("```"):
        raw = raw[7:-3].strip()
    elif raw.startswith("```") and raw.endswith("```"):
        raw = raw[3:-3].strip()
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("review must be one JSON object") from error
    if not isinstance(payload, dict):
        raise ValueError("review must be one JSON object")
    decision = payload.get("decision")
    if decision == "DONE":
        if payload.get("terminal") != "SUPERVISOR_DONE":
            raise ValueError("DONE review must carry terminal=SUPERVISOR_DONE")
        return "DONE", None
    if decision == "CONTINUE":
        next_prompt = payload.get("next_prompt")
        if not isinstance(next_prompt, str) or not next_prompt.strip():
            raise ValueError("CONTINUE review must carry a non-empty next_prompt")
        return "CONTINUE", next_prompt.strip()
    raise ValueError("review decision must be CONTINUE or DONE")


def _visible_progress(before: PageSnapshot, current: PageSnapshot) -> bool:
    if current.assistant_turn_id and current.assistant_turn_id != before.assistant_turn_id:
        return True
    return bool(
        current.assistant_text_signature
        and current.assistant_text_signature != before.assistant_text_signature
    )


@dataclass
class SimpleWatcher:
    """Conservative periodic nudge loop for one exact conversation URL."""

    target_url: str
    relay_url: str = "http://127.0.0.1:9224"
    sleep: Callable[[float], None] = time.sleep
    render_wait_seconds: float = RENDER_WAIT_SECONDS
    submission_confirm_seconds: float = SUBMISSION_CONFIRM_SECONDS
    progress_poll_seconds: float = PROGRESS_POLL_SECONDS
    progress_window_seconds: float = PROGRESS_WINDOW_SECONDS
    liveness_timeout_seconds: float = SIMPLE_INACTIVITY_TIMEOUT_SECONDS
    clock: Callable[[], float] = time.monotonic
    page_factory: Callable[..., RelayChatGPTPage] | None = None
    open_page: Callable[[str, str], RelayChatGPTPage] | None = None
    observation_client: SidecarObservationClient | None = None
    intent_client: SidecarIntentClient | None = None
    legacy_direct_send: bool = False
    registration_id: str | None = None
    wall_clock: Callable[[], float] = time.time
    fixed_prompt_interval_seconds: float = 900.0

    def __post_init__(self) -> None:
        self.conversation_id = conversation_id_from_url(self.target_url)
        self.fixed_prompt_interval_seconds = _positive_fixed_time(
            self.fixed_prompt_interval_seconds, "fixed_prompt_interval_seconds",
        )
        self._fixed_prompt_next_due_at: float | None = None
        if self.page_factory is None and not self.legacy_direct_send:
            self.observation_client = self.observation_client or SidecarObservationClient()
            self.intent_client = self.intent_client or SidecarIntentClient()
        elif self.observation_client is not None or self.intent_client is not None:
            raise ValueError("managed observations cannot share a direct page factory")
        if self.legacy_direct_send and self.page_factory is None:
            self.page_factory = RelayChatGPTPage.connect
        self._state = "waiting"
        self._completion_text: str | None = None
        self._diagnostics: dict[str, object] = {}
        self._phase = 0
        self._cycle = 0
        self._next_prompt = ""
        self._run_status = RUNNING_STATUS
        self._done_latched = False
        self._need_input_latched = False
        self._need_input_user_turn_id: str | None = None
        self._last_active_key: tuple[str, str] | None = None
        self._last_active_kind: str | None = None
        self._last_active_at: float | None = None
        self._liveness_attempted_for: tuple[str, str] | None = None
        self._transition_attempted_for: str | None = None
        self._expected_review_intent_id: str | None = None
        self._legacy_transition_state = False
        self._persistence_callback: Callable[[dict[str, object]], None] | None = None

    @property
    def should_stop(self) -> bool:
        # Lifecycle is externally owned. A latched DONE pauses automatic nudges
        # but deliberately keeps the durable watch registered until an explicit
        # unregister/re-register lifecycle action occurs.
        return False

    @property
    def state(self) -> str:
        return self._state

    @property
    def completion_text(self) -> str | None:
        return self._completion_text

    @property
    def durable_state(self) -> dict[str, object]:
        state = {
            "phase": self._phase,
            "cycle": self._cycle,
            "next_prompt": self._next_prompt,
            "status": self._run_status,
            "transition_turn_id": self._transition_attempted_for,
            "expected_review_intent_id": self._expected_review_intent_id,
            "liveness_attempted_for": (None if self._liveness_attempted_for is None
                                       else list(self._liveness_attempted_for)),
        }
        if self.observation_client is not None and self._fixed_prompt_next_due_at is not None:
            state["fixed_prompt_next_due_at"] = self._fixed_prompt_next_due_at
        # Absence carries the unresolved legacy fence across regular registry
        # checkpoints and owner rebinds. Modern human-input release keeps an
        # explicit null, so a new ACTION is never mistaken for that old REVIEW.
        if self._legacy_transition_state and self._transition_attempted_for is None:
            state.pop("transition_turn_id")
        return state

    def restore_state(self, value: dict[str, object]) -> None:
        base_keys = {"phase", "cycle", "next_prompt", "status"}
        optional_keys = {"transition_turn_id", "expected_review_intent_id", "liveness_attempted_for",
                         "fixed_prompt_next_due_at"}
        if not base_keys <= set(value) or set(value) - (base_keys | optional_keys):
            raise ValueError("invalid simple watchdog durable state fields")
        fixed_due = (_positive_fixed_time(value["fixed_prompt_next_due_at"], "fixed_prompt_next_due_at")
                     if "fixed_prompt_next_due_at" in value else None)
        transition_turn_id = value.get("transition_turn_id")
        if transition_turn_id is not None and (not isinstance(transition_turn_id, str) or not transition_turn_id):
            raise ValueError("transition_turn_id must be a non-empty string or null")
        expected_review_intent_id = value.get("expected_review_intent_id")
        if expected_review_intent_id is not None and (not isinstance(expected_review_intent_id, str) or not expected_review_intent_id):
            raise ValueError("expected_review_intent_id must be a non-empty string or null")
        liveness = value.get("liveness_attempted_for")
        if liveness is not None and (not isinstance(liveness, (list, tuple)) or len(liveness) != 2
                                     or any(not isinstance(part, str) for part in liveness)):
            raise ValueError("liveness_attempted_for must be a two-string tuple or null")
        phase = value["phase"]
        cycle = value["cycle"]
        next_prompt = value["next_prompt"]
        status = value["status"]
        if phase not in (0, 1):
            raise ValueError("phase must be 0 or 1")
        if not isinstance(cycle, int) or isinstance(cycle, bool) or cycle < 0:
            raise ValueError("cycle must be a non-negative integer")
        if not isinstance(next_prompt, str):
            raise ValueError("next_prompt must be a string")
        if status not in (RUNNING_STATUS, DONE_STATUS):
            raise ValueError("status must be RUNNING or DONE")
        self._phase = phase
        self._fixed_prompt_next_due_at = fixed_due
        self._cycle = cycle
        self._next_prompt = next_prompt
        self._run_status = status
        self._transition_attempted_for = transition_turn_id
        self._legacy_transition_state = "transition_turn_id" not in value
        self._expected_review_intent_id = expected_review_intent_id
        self._liveness_attempted_for = None if liveness is None else tuple(liveness)
        self._done_latched = status == DONE_STATUS
        if self._done_latched:
            self._state = "done"

    def set_persistence_callback(self, callback) -> None:
        self._persistence_callback = callback

    def _fixed_prompt_due(self) -> bool:
        return (self.observation_client is not None
                and self._fixed_prompt_next_due_at is not None
                and self.wall_clock() >= self._fixed_prompt_next_due_at)

    def _advance_fixed_prompt(self) -> None:
        if self._fixed_prompt_due():
            elapsed = self.wall_clock() - self._fixed_prompt_next_due_at
            slots = math.floor(elapsed / self.fixed_prompt_interval_seconds) + 1
            self._fixed_prompt_next_due_at += slots * self.fixed_prompt_interval_seconds

    def _arm_fixed_prompt(self) -> bool:
        if self.observation_client is None or self._fixed_prompt_next_due_at is not None:
            return True
        self._fixed_prompt_next_due_at = _positive_fixed_time(
            self.wall_clock() + self.fixed_prompt_interval_seconds, "fixed_prompt_next_due_at",
        )
        try:
            if self._persistence_callback is not None:
                self._persistence_callback(self.durable_state)
        except Exception as error:
            self._fixed_prompt_next_due_at = None
            self._state = "fixed_prompt_storage_unavailable"
            self._diagnostics = {"reason": "fixed_prompt_arm_failed", "error": str(error)[:1000]}
            return False
        return True

    def _send_fixed_prompt(self, page, before) -> str:
        previous_due = self._fixed_prompt_next_due_at
        previous_transition = self._transition_attempted_for
        previous_review = self._expected_review_intent_id
        self._advance_fixed_prompt()
        self._transition_attempted_for = before.turn_key
        try:
            if self._persistence_callback is not None:
                self._persistence_callback(self.durable_state)
        except Exception as error:
            self._fixed_prompt_next_due_at = previous_due
            self._transition_attempted_for = previous_transition
            self._state = "fixed_prompt_storage_unavailable"
            self._diagnostics = {"reason": "fixed_prompt_reservation_failed", "error": str(error)[:1000]}
            return self._state
        delivery = page.send_fixed_prompt(
            REVIEW_FIXED_PROMPT if self._phase == 1 else ACTION_FIXED_PROMPT,
            before.turn_key, stop_timeout=min(10.0, self.submission_confirm_seconds),
            before_continue=self._reserve_review_recovery if self._phase == 1 else None,
        )
        if not delivery.accepted and not delivery.uncertain:
            self._transition_attempted_for = previous_transition
            self._expected_review_intent_id = previous_review
            try:
                if self._persistence_callback is not None:
                    self._persistence_callback(self.durable_state)
            except Exception as error:
                self._state = "fixed_prompt_storage_unavailable"
                self._diagnostics = {"reason": "fixed_prompt_rejection_checkpoint_failed",
                                     "error": str(error)[:1000]}
                return self._state
        self._state = ("fixed_prompt_sent" if delivery.accepted else
                       "fixed_prompt_unknown" if delivery.uncertain else
                       "fixed_prompt_stale" if delivery.stale else "fixed_prompt_rejected")
        self._diagnostics = {"frontend_user_message_id": delivery.message_id}
        return self._state

    def _fixed_prompt_eligible(self, before) -> bool:
        return bool(self._fixed_prompt_due() and before.turn_key and before.user_turn_id
                    and not before.interaction_required and not before.user_turn_pending
                    and not before.send_timeout and not before.fault_text)

    def _checkpoint_liveness(self, key) -> bool:
        previous = self._liveness_attempted_for
        self._liveness_attempted_for = key
        try:
            if self._persistence_callback is not None:
                self._persistence_callback(self.durable_state)
        except Exception as error:
            self._liveness_attempted_for = previous
            self._state = "liveness_storage_unavailable"
            self._diagnostics = {"reason": "liveness_reservation_failed", "error": str(error)[:1000]}
            return False
        return True

    def _reserve_review_recovery(self, intent_id) -> None:
        previous = self._expected_review_intent_id
        self._expected_review_intent_id = intent_id
        try:
            if self._persistence_callback is not None:
                self._persistence_callback(self.durable_state)
        except Exception:
            self._expected_review_intent_id = previous
            raise

    def bind_registration(self, registration_id) -> None:
        if self.intent_client is not None:
            self.intent_client.bind_watch(registration_id, self.target_url)
        self.registration_id = registration_id

    @staticmethod
    def _owner_binding_required(diagnostics) -> bool:
        return any(diagnostics.get(key) == "watchdog_binding_required"
                   for key in ("intent_reason", "recovery_reason"))

    @property
    def requires_owner_rebind(self) -> bool:
        return self._owner_binding_required(self._diagnostics)

    @property
    def diagnostics(self) -> dict:
        fixed = ({"fixed_prompt_next_due_at": self._fixed_prompt_next_due_at,
                  "fixed_prompt_due": self._fixed_prompt_due()}
                 if self.observation_client is not None else {})
        return {**self._diagnostics, **self.durable_state, **fixed}

    def _connect_or_open(self) -> tuple[RelayChatGPTPage, bool]:
        # Polling observes an existing exact tab only. A missing target is a
        # transient browser/relay condition; opening a replacement here can
        # fan out duplicate ChatGPT tabs while the real tab is merely hidden.
        if self.observation_client is not None:
            return SidecarObservationPage(
                self.target_url, self.observation_client, self.intent_client, sleep=self.sleep,
                registration_id=self.registration_id,
            ), False
        match = f"/c/{self.conversation_id}"
        return self.page_factory(self.relay_url, match), False

    def _open_exact_page(self, url: str, match: str) -> RelayChatGPTPage:
        ws_url = discover_websocket_url(self.relay_url)
        import websocket

        socket = websocket.create_connection(ws_url, timeout=3.0, suppress_origin=True)
        try:
            RelayCdpProtocol(socket).create_target(url)
        finally:
            with suppress(Exception):
                socket.close()

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                return self.page_factory(self.relay_url, match)
            except RelayTargetSelectionError as error:
                if "no matching ChatGPT target" not in str(error):
                    raise
                self.sleep(0.25)
        raise RuntimeError("new exact conversation tab did not appear")

    def step(self) -> str:
        # DONE is a terminal semantic state even when registry lifecycle remains
        # externally owned. Once observed, do not reconnect, refresh, or send
        # periodic continuation prompts on later ticks.
        if self._done_latched or self._run_status == DONE_STATUS:
            self._state = "done"
            return self._state
        if not self._arm_fixed_prompt():
            return self._state

        try:
            page, opened = self._connect_or_open()
        except (ObservationUnavailable, ObservationProtocolError) as error:
            self._state = "observation_unavailable"
            self._last_active_at = None
            self._diagnostics = {"reason": str(error), "observation_available": False}
            return self._state
        except RelayTargetSelectionError as error:
            if "no matching ChatGPT target" not in str(error):
                raise
            self._state = "reconnecting"
            self._diagnostics = {"reason": "target_missing_no_tab_open"}
            return self._state
        try:
            if opened:
                self.sleep(self.render_wait_seconds)

            current_url = page.current_url()
            if not _same_conversation(current_url, self.target_url):
                self._state = "target_changed"
                return self._state

            before = page.snapshot()
            self._completion_text = before.assistant_text

            if self._need_input_latched:
                current_user_turn_id = before.user_turn_id or ""
                latched_user_turn_id = self._need_input_user_turn_id or ""
                if (not current_user_turn_id or current_user_turn_id == latched_user_turn_id
                        or (self.observation_client is not None
                            and not page.proves_user_turn(current_user_turn_id))):
                    self._state = "need_input"
                    return self._state

                # A new human turn releases NEED_INPUT, but this scheduler tick
                # remains observation-only so the watchdog cannot race the
                # assistant response to that human input.
                self._need_input_latched = False
                self._need_input_user_turn_id = None
                self._last_active_key = None
                self._last_active_at = None
                self._liveness_attempted_for = None
                self._transition_attempted_for = None
                if self._phase == 0:
                    self._legacy_transition_state = False
                self._state = "human_input_observed"
                return self._state

            # The visible assistant body can still belong to the previous
            # user turn while a newer human turn is already pending. Never
            # re-latch a stale NEED_INPUT marker over that newer work.
            terminal = None if before.user_turn_pending else _terminal_status(before.assistant_text)
            if terminal == "need_input":
                self._state = terminal
                self._need_input_latched = True
                self._need_input_user_turn_id = before.user_turn_id or ""
                return self._state

            if (self.observation_client is not None and before.turn_key
                    and before.turn_key == self._transition_attempted_for):
                # A fixed Stop reservation can survive a crash while this same
                # owned REVIEW naturally finishes. Its final DONE may latch,
                # while every continuation remains fenced on the consumed turn.
                if (self._phase == 1 and before.phase is Phase.FINISHED
                        and page.review_lineage_matches(
                            self._expected_review_intent_id, self.registration_id)):
                    try:
                        review_decision, _ = _parse_review(before.assistant_text)
                    except ValueError:
                        pass
                    else:
                        if review_decision == "DONE":
                            self._run_status = DONE_STATUS
                            self._done_latched = True
                            self._state = "done"
                            self._diagnostics = {"review_decision": "DONE"}
                            return self._state
                self._state = "transition_pending"
                self._diagnostics = {"reason": "awaiting_owned_turn_or_explicit_reregister"}
                return self._state
            if self._phase == 1 and self.observation_client is not None:
                if not page.review_lineage_matches(self._expected_review_intent_id, self.registration_id):
                    self._state = "need_input"
                    self._diagnostics = {"reason": "review_lineage_unverified",
                                         "required_action": "explicit_unregister_then_register_to_reset_phase"}
                    return self._state

            if (self.observation_client is not None and self._phase == 0 and self._next_prompt
                    and self._legacy_transition_state and not self._transition_attempted_for):
                try:
                    _parse_review(before.assistant_text)
                except ValueError:
                    pass
                else:
                    self._state = "transition_pending"
                    self._diagnostics = {"reason": "prior_review_still_visible"}
                    return self._state
            if before.phase is not Phase.FINISHED and self._fixed_prompt_eligible(before):
                return self._send_fixed_prompt(page, before)

            # The fixed fallback above owns its Stop-and-fresh-check path.
            # Existing liveness recovery below keeps its inactivity guards;
            # normal phase progression still requires positive finality.
            if before.phase is not Phase.FINISHED:
                if before.user_turn_pending:
                    self._last_active_key = None
                    self._last_active_at = None
                    self._liveness_attempted_for = None
                    self._state = "waiting_for_assistant"
                    return self._state

                if before.phase in (Phase.THINKING, Phase.RESPONDING):
                    progress_key = None
                    if (
                        before.assistant_turn_id
                        and before.assistant_text_signature
                        and before.assistant_text.strip()
                    ):
                        progress_key = (
                            before.turn_key,
                            before.assistant_text_signature,
                        )
                    now = self.clock()
                    if progress_key is None:
                        self._last_active_key = None
                        self._last_active_at = None
                        self._liveness_attempted_for = None
                        self._state = "active"
                        return self._state

                    if (progress_key != self._last_active_key or self._last_active_kind != "active"
                            or self._last_active_at is None):
                        # Any newly visible assistant content restarts the
                        # 15-minute inactivity window. Observation cadence is
                        # intentionally independent from this timer.
                        self._last_active_key = progress_key
                        self._last_active_kind = "active"
                        self._last_active_at = now
                        if self._liveness_attempted_for != progress_key:
                            self._liveness_attempted_for = None
                        self._state = "active"
                        self._diagnostics = {
                            "inactivity_seconds": 0.0,
                            "inactivity_timeout_seconds": self.liveness_timeout_seconds,
                            "assistant_turn_key": before.turn_key,
                        }
                        return self._state

                    if (
                        self._last_active_at is not None
                        and now - self._last_active_at >= self.liveness_timeout_seconds
                        and self._liveness_attempted_for != progress_key
                    ):
                        # Consume this exact stagnant state before attempting an
                        # effect. A lost ACK must never cause the same active
                        # turn/signature to receive a second liveness prompt.
                        if not self._checkpoint_liveness(progress_key):
                            return self._state
                        previous_review_intent_id = self._expected_review_intent_id
                        recovery_kwargs = {}
                        if self._phase == 1 and self.observation_client is not None:
                            recovery_kwargs["before_continue"] = self._reserve_review_recovery
                        recovery_prompt = (
                            REVIEW_RECOVERY_PROMPT if self._phase == 1 else ACTION_RECOVERY_PROMPT
                        )
                        delivery = page.recover_stalled_active(
                            recovery_prompt,
                            before.turn_key,
                            stop_timeout=min(10.0, self.submission_confirm_seconds),
                            acceptance_timeout=self.submission_confirm_seconds,
                            **recovery_kwargs,
                        )
                        if not delivery.accepted and not delivery.uncertain:
                            self._expected_review_intent_id = previous_review_intent_id
                            if self._owner_binding_required(getattr(page, "diagnostics", {})):
                                self._liveness_attempted_for = None
                        self._diagnostics = {
                            "liveness_turn_key": before.turn_key,
                            "liveness_signature": before.assistant_text_signature,
                            "frontend_user_message_id": delivery.message_id,
                        }
                        if delivery.accepted:
                            self._state = "liveness_recovery_sent"
                        elif delivery.uncertain:
                            self._state = "liveness_recovery_unknown"
                        elif delivery.stale:
                            self._state = "liveness_recovery_stale"
                        else:
                            self._state = "liveness_recovery_rejected"
                        return self._state

                    self._state = "active"
                    self._diagnostics = {
                        "inactivity_seconds": (
                            None
                            if self._last_active_at is None
                            else max(0.0, now - self._last_active_at)
                        ),
                        "inactivity_timeout_seconds": self.liveness_timeout_seconds,
                        "assistant_turn_key": before.turn_key,
                    }
                    return self._state

                if before.phase is Phase.BLOCKED:
                    # Only the managed positive incomplete projection grants a
                    # short recovery window. Other blocked states keep the fallback.
                    incomplete = self.observation_client is not None and before.stream_interrupted
                    recovery_timeout = (min(self.liveness_timeout_seconds, 30.0)
                                        if incomplete else self.liveness_timeout_seconds)
                    block_key = (before.turn_key or "", before.assistant_text_signature or "")
                    block_kind = "incomplete" if incomplete else "blocked"
                    now = self.clock()
                    if (block_key != self._last_active_key or self._last_active_kind != block_kind
                            or self._last_active_at is None):
                        self._last_active_key = block_key
                        self._last_active_kind = block_kind
                        self._last_active_at = now
                        if self._liveness_attempted_for != block_key:
                            self._liveness_attempted_for = None
                        self._state = "blocked"
                        self._diagnostics = {
                            "inactivity_seconds": 0.0,
                            "inactivity_timeout_seconds": recovery_timeout,
                            "assistant_turn_key": before.turn_key,
                        }
                        return self._state
                    if (incomplete and self._last_active_at is not None
                            and now - self._last_active_at >= recovery_timeout
                            and self._liveness_attempted_for != block_key):
                        if not self._checkpoint_liveness(block_key):
                            return self._state
                        previous_review_intent_id = self._expected_review_intent_id
                        recovery_kwargs = {}
                        if self._phase == 1:
                            recovery_kwargs["before_continue"] = self._reserve_review_recovery
                        delivery = page.recover_incomplete(
                            REVIEW_RECOVERY_PROMPT if self._phase == 1 else ACTION_RECOVERY_PROMPT,
                            before.turn_key, acceptance_timeout=self.submission_confirm_seconds,
                            **recovery_kwargs,
                        )
                        if not delivery.accepted and not delivery.uncertain:
                            self._expected_review_intent_id = previous_review_intent_id
                            if self._owner_binding_required(getattr(page, "diagnostics", {})):
                                self._liveness_attempted_for = None
                        self._diagnostics = {
                            "liveness_turn_key": before.turn_key,
                            "liveness_signature": before.assistant_text_signature,
                            "frontend_user_message_id": delivery.message_id,
                            "inactivity_timeout_seconds": recovery_timeout,
                        }
                        if delivery.accepted:
                            self._state = "liveness_recovery_sent"
                        elif delivery.uncertain:
                            self._state = "liveness_recovery_unknown"
                        elif delivery.stale:
                            self._state = "liveness_recovery_stale"
                        else:
                            self._state = "liveness_recovery_rejected"
                        return self._state
                    if (
                        not before.interaction_required
                        and not before.send_timeout
                        and not before.stream_interrupted
                        and self._last_active_at is not None
                        and now - self._last_active_at >= self.liveness_timeout_seconds
                        and self._liveness_attempted_for != block_key
                    ):
                        if not self._checkpoint_liveness(block_key):
                            return self._state
                        refresh = page.refresh()
                        if (refresh is not None and not refresh.accepted and not refresh.uncertain
                                and self._owner_binding_required(getattr(page, "diagnostics", {}))):
                            self._liveness_attempted_for = None
                        if refresh is None or refresh.accepted:
                            self._state = "blocked_recovery_refresh"
                        elif refresh.uncertain:
                            self._state = "blocked_recovery_unknown"
                        elif refresh.stale:
                            self._state = "blocked_recovery_stale"
                        else:
                            self._state = "blocked_recovery_rejected"
                        self._diagnostics = {
                            "inactivity_seconds": max(0.0, now - self._last_active_at),
                            "inactivity_timeout_seconds": self.liveness_timeout_seconds,
                            "assistant_turn_key": before.turn_key,
                        }
                        return self._state
                    self._state = "blocked"
                    self._diagnostics = {
                        "inactivity_seconds": (
                            None
                            if self._last_active_at is None
                            else max(0.0, now - self._last_active_at)
                        ),
                        "inactivity_timeout_seconds": recovery_timeout,
                        "assistant_turn_key": before.turn_key,
                    }
                    return self._state

                self._last_active_key = None
                self._last_active_at = None
                self._liveness_attempted_for = None
                self._state = "blocked"
                return self._state

            self._last_active_key = None
            self._last_active_at = None
            self._liveness_attempted_for = None
            if before.turn_key and self._transition_attempted_for == before.turn_key:
                self._state = "transition_pending"
                self._diagnostics = {"reason": "awaiting_new_assistant_turn_or_explicit_reregister"}
                return self._state

            # Older durable rows have no consumed turn fence. For those rows,
            # phase=0 plus next_prompt and a still-visible REVIEW payload must
            # fail closed. Current rows use the exact durable turn comparison
            # above, so a new ACTION may finish with any body, including JSON.
            if (self._phase == 0 and self._next_prompt and self._legacy_transition_state
                    and not self._transition_attempted_for):
                try:
                    _parse_review(before.assistant_text)
                except ValueError:
                    pass
                else:
                    self._state = "transition_pending"
                    self._diagnostics = {"reason": "prior_review_still_visible"}
                    return self._state

            previous_state = self.durable_state
            if self._phase == 0:
                # ACTION has no terminal authority. Even if its body contains a
                # DONE marker, natural finality always transitions to REVIEW.
                prompt = REVIEW_PROMPT.format(cycle=self._cycle)
                transition_phase = 1
                transition_cycle = self._cycle
            else:
                try:
                    review_decision, review_next_prompt = _parse_review(before.assistant_text)
                except ValueError as error:
                    if self._fixed_prompt_eligible(before):
                        return self._send_fixed_prompt(page, before)
                    self._state = "review_invalid"
                    self._diagnostics = {"review_error": str(error)}
                    return self._state
                if review_decision == "DONE":
                    self._run_status = DONE_STATUS
                    self._done_latched = True
                    self._state = "done"
                    self._diagnostics = {"review_decision": "DONE"}
                    return self._state
                prompt = review_next_prompt or ""
                self._next_prompt = prompt
                transition_phase = 0
                transition_cycle = self._cycle + 1

            # Reserve semantic authority before the owner can execute the
            # browser effect. The durable consumed turn fence survives a crash
            # between the owner receipt and the registry's regular observation.
            if transition_phase == 1 and self.observation_client is not None:
                try:
                    expected_review_intent_id = page.prepare_simple_continue(prompt, before.turn_key)
                except Exception as error:
                    self.restore_state(previous_state)
                    self._state = "send_rejected"
                    self._diagnostics = {"reason": "review_intent_preparation_failed", "error": str(error)[:1000]}
                    return self._state
            else:
                expected_review_intent_id = None
            self._expected_review_intent_id = expected_review_intent_id
            self._phase = transition_phase
            self._cycle = transition_cycle
            self._transition_attempted_for = before.turn_key or None
            self._advance_fixed_prompt()
            if self._persistence_callback is not None:
                try:
                    self._persistence_callback(self.durable_state)
                except Exception as error:
                    self.restore_state(previous_state)
                    self._state = "transition_storage_unavailable"
                    self._diagnostics = {"reason": "transition_reservation_failed", "error": str(error)[:1000]}
                    return self._state
            delivery = page.send_simple_continue(
                prompt,
                before.turn_key,
                acceptance_timeout=0.0,
            )
            if not delivery.accepted and not delivery.uncertain:
                # A definitive rejection proves no new semantic phase was
                # accepted. Unknown delivery retains the durable reservation.
                consumed_due = self._fixed_prompt_next_due_at
                self.restore_state(previous_state)
                self._fixed_prompt_next_due_at = consumed_due
            if not delivery.accepted:
                self._state = "submission_unknown" if delivery.uncertain else "send_rejected"
                return self._state

            # Normal progression is scheduler-driven, not an inner wait loop.
            # Return immediately so RegistryStore can persist the new phase and
            # the 15s observation loop can inspect browser progress independently.
            self._state = "transition_sent"
            self._diagnostics = {"frontend_user_message_id": delivery.message_id}
            return self._state
        finally:
            self._diagnostics.update(getattr(page, "diagnostics", {}))
            page.close()

    def close(self) -> None:
        # Each tick owns and closes its own transient Relay attachment.
        return None
