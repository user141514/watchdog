from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
import json
import logging
import time
from typing import Callable

from .model import PageSnapshot, Phase, is_need_input
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
    page_factory: Callable[..., RelayChatGPTPage] = RelayChatGPTPage.connect
    open_page: Callable[[str, str], RelayChatGPTPage] | None = None

    def __post_init__(self) -> None:
        self.conversation_id = conversation_id_from_url(self.target_url)
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
        self._last_active_at: float | None = None
        self._liveness_attempted_for: tuple[str, str] | None = None
        self._transition_attempted_for: str | None = None

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
        return {
            "phase": self._phase,
            "cycle": self._cycle,
            "next_prompt": self._next_prompt,
            "status": self._run_status,
        }

    def restore_state(self, value: dict[str, object]) -> None:
        if set(value) != {"phase", "cycle", "next_prompt", "status"}:
            raise ValueError("simple watchdog state must contain exactly phase/cycle/next_prompt/status")
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
        self._cycle = cycle
        self._next_prompt = next_prompt
        self._run_status = status
        self._done_latched = status == DONE_STATUS
        if self._done_latched:
            self._state = "done"

    @property
    def diagnostics(self) -> dict:
        return {**self._diagnostics, **self.durable_state}

    def _connect_or_open(self) -> tuple[RelayChatGPTPage, bool]:
        # Polling observes an existing exact tab only. A missing target is a
        # transient browser/relay condition; opening a replacement here can
        # fan out duplicate ChatGPT tabs while the real tab is merely hidden.
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

        try:
            page, opened = self._connect_or_open()
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
                if not current_user_turn_id or current_user_turn_id == latched_user_turn_id:
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

            # Simple mode is deliberately conservative: never double-text an
            # active assistant, race a newer user turn, or push through a
            # blocked/interaction state. The periodic nudge is only legal after
            # positive assistant finality has produced Phase.FINISHED.
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

                    if progress_key != self._last_active_key:
                        # Any newly visible assistant content restarts the
                        # 15-minute inactivity window. Observation cadence is
                        # intentionally independent from this timer.
                        self._last_active_key = progress_key
                        self._last_active_at = now
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
                        self._liveness_attempted_for = progress_key
                        recovery_prompt = (
                            REVIEW_RECOVERY_PROMPT if self._phase == 1 else ACTION_RECOVERY_PROMPT
                        )
                        delivery = page.recover_stalled_active(
                            recovery_prompt,
                            before.turn_key,
                            stop_timeout=min(10.0, self.submission_confirm_seconds),
                            acceptance_timeout=self.submission_confirm_seconds,
                        )
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
                    # BLOCKED without an explicit human/transport gate is an
                    # observation failure, not permission for immediate repair.
                    # It shares the 900s fallback clock with active stalls.
                    block_key = (before.turn_key or "", before.assistant_text_signature or "")
                    now = self.clock()
                    if block_key != self._last_active_key:
                        self._last_active_key = block_key
                        self._last_active_at = now
                        self._liveness_attempted_for = None
                        self._state = "blocked"
                        self._diagnostics = {
                            "inactivity_seconds": 0.0,
                            "inactivity_timeout_seconds": self.liveness_timeout_seconds,
                            "assistant_turn_key": before.turn_key,
                        }
                        return self._state
                    if (
                        not before.interaction_required
                        and not before.send_timeout
                        and not before.stream_interrupted
                        and self._last_active_at is not None
                        and now - self._last_active_at >= self.liveness_timeout_seconds
                        and self._liveness_attempted_for != block_key
                    ):
                        self._liveness_attempted_for = block_key
                        page.refresh()
                        self._state = "blocked_recovery_refresh"
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
                        "inactivity_timeout_seconds": self.liveness_timeout_seconds,
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
                return self._state

            # After REVIEW->ACTION, phase=0 plus a non-empty next_prompt is the
            # durable evidence that an ACTION transition was already consumed.
            # A daemon restart loses the ephemeral turn latch; if the still-
            # visible finished body is itself a valid REVIEW payload, fail
            # closed instead of misclassifying it as a completed ACTION and
            # sending REVIEW twice.
            if self._phase == 0 and self._next_prompt:
                try:
                    _parse_review(before.assistant_text)
                except ValueError:
                    pass
                else:
                    self._state = "transition_pending"
                    self._diagnostics = {"reason": "prior_review_still_visible"}
                    return self._state

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

            delivery = page.send_simple_continue(
                prompt,
                before.turn_key,
                acceptance_timeout=0.0,
            )
            if delivery.accepted or delivery.uncertain:
                # An uncertain frontend receipt is consumed as an attempted
                # transition. This fails closed instead of replaying a prompt
                # that may already have been accepted by the browser.
                self._phase = transition_phase
                self._cycle = transition_cycle
                self._transition_attempted_for = before.turn_key or None
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
            page.close()

    def close(self) -> None:
        # Each tick owns and closes its own transient Relay attachment.
        return None
