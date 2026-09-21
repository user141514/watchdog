from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re


TurnKey = str


class Phase(str, Enum):
    THINKING = "thinking"
    RESPONDING = "responding"
    FINISHED = "finished"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class DomSignals:
    stop_visible: bool
    assistant_busy: bool
    thinking_visible: bool
    composer_ready: bool
    composer_has_draft: bool = False
    assistant_present: bool = True
    interaction_required: bool = False
    send_timeout: bool = False
    stream_interrupted: bool = False
    assistant_finalized: bool = False
    user_turn_pending: bool = False


@dataclass(frozen=True)
class PageSnapshot:
    phase: Phase
    assistant_turn_id: str
    assistant_text_signature: str
    assistant_text: str
    assistant_count: int
    user_count: int
    user_turn_id: str = ""
    user_text: str = ""
    stop_visible: bool = False
    composer_has_draft: bool = False
    submission_seq: int = 0
    submission_receipt_seq: int = 0
    submission_receipt_id: str = ""
    submission_receipt_text: str = ""
    trusted_submission_receipt_seq: int = 0
    trusted_submission_receipt_id: str = ""
    user_turn_pending: bool = False
    interaction_required: bool = False
    send_timeout: bool = False
    stream_interrupted: bool = False
    fault_text: str = ""

    @property
    def turn_key(self) -> TurnKey:
        """Stable assistant-turn identity used by semantic control paths.

        DOM message cardinality is viewport metadata: ChatGPT virtualizes old
        turns, so assistant_count/user_count are deliberately excluded.
        """
        return self.assistant_turn_id


@dataclass(frozen=True)
class PromptDelivery:
    accepted: bool
    message_id: str = ""
    stale: bool = False
    uncertain: bool = False

    def __bool__(self) -> bool:
        return self.accepted


def classify_phase(signals: DomSignals) -> Phase:
    if signals.interaction_required or signals.send_timeout or signals.stream_interrupted:
        return Phase.BLOCKED
    generation_active = signals.stop_visible or signals.assistant_busy
    if generation_active:
        return Phase.THINKING if signals.thinking_visible else Phase.RESPONDING
    if (
        signals.composer_ready
        and not signals.composer_has_draft
        and signals.assistant_present
        and signals.assistant_finalized
        and not signals.user_turn_pending
    ):
        return Phase.FINISHED
    return Phase.BLOCKED


_DONE_RE = re.compile(
    r"(?:\bSUPERVISOR_DONE\b|\[SUPERVISOR_STATE\s*:\s*DONE\])",
    re.IGNORECASE,
)
_NEED_INPUT_RE = re.compile(
    r"(?:^|\n)\[SUPERVISOR_STATE\s*:\s*NEED_INPUT\]\s*\Z",
    re.IGNORECASE,
)


def is_done(text: str) -> bool:
    return bool(_DONE_RE.search(text))


def is_need_input(text: str) -> bool:
    return bool(_NEED_INPUT_RE.search(text))
