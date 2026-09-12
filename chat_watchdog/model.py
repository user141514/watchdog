from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re


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
    interaction_required: bool = False
    send_timeout: bool = False
    stream_interrupted: bool = False
    fault_text: str = ""

    @property
    def turn_key(self) -> tuple[int, str]:
        return (
            self.assistant_count,
            self.assistant_turn_id,
        )


@dataclass(frozen=True)
class PromptDelivery:
    accepted: bool
    message_id: str = ""


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
    ):
        return Phase.FINISHED
    return Phase.BLOCKED


_DONE_RE = re.compile(
    r"(?:\bSUPERVISOR_DONE\b|\[SUPERVISOR_STATE\s*:\s*DONE\])",
    re.IGNORECASE,
)


def is_done(text: str) -> bool:
    return bool(_DONE_RE.search(text))
