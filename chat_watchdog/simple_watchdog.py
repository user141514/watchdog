from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
import logging
import time
from typing import Callable

from .model import PageSnapshot, is_done, is_need_input
from .registry import conversation_id_from_url
from .relay_cdp import RelayCdpProtocol
from .relay_page import (
    RelayChatGPTPage,
    RelayTargetSelectionError,
    discover_websocket_url,
)
from .supervisor import CONTINUE_PROMPT


_LOG = logging.getLogger(__name__)

RENDER_WAIT_SECONDS = 60.0
SUBMISSION_CONFIRM_SECONDS = 90.0
PROGRESS_POLL_SECONDS = 10.0
PROGRESS_WINDOW_SECONDS = 90.0
DEFAULT_SIMPLE_INTERVAL_SECONDS = 15 * 60


def _same_conversation(left: str, right: str) -> bool:
    try:
        return conversation_id_from_url(left) == conversation_id_from_url(right)
    except ValueError:
        return False


def _terminal_status(text: str) -> str | None:
    tail = (text or "")[-4096:]
    if is_need_input(tail):
        return "need_input"
    if is_done(tail):
        return "done"
    return None


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
    page_factory: Callable[..., RelayChatGPTPage] = RelayChatGPTPage.connect
    open_page: Callable[[str, str], RelayChatGPTPage] | None = None

    def __post_init__(self) -> None:
        self.conversation_id = conversation_id_from_url(self.target_url)
        self._state = "waiting"
        self._completion_text: str | None = None
        self._diagnostics: dict[str, object] = {}

    @property
    def should_stop(self) -> bool:
        # Lifecycle is externally owned. DONE/NEED_INPUT only suppress this tick.
        return False

    @property
    def state(self) -> str:
        return self._state

    @property
    def completion_text(self) -> str | None:
        return self._completion_text

    @property
    def diagnostics(self) -> dict:
        return dict(self._diagnostics)

    def _connect_or_open(self) -> tuple[RelayChatGPTPage, bool]:
        match = f"/c/{self.conversation_id}"
        try:
            return self.page_factory(self.relay_url, match), False
        except RelayTargetSelectionError as error:
            if "no matching ChatGPT target" not in str(error):
                raise
        opener = self.open_page or self._open_exact_page
        return opener(self.target_url, match), True

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
        page, opened = self._connect_or_open()
        try:
            if not opened:
                page.refresh()
            self.sleep(self.render_wait_seconds)

            current_url = page.current_url()
            if not _same_conversation(current_url, self.target_url):
                self._state = "target_changed"
                return self._state

            before = page.snapshot()
            self._completion_text = before.assistant_text
            terminal = _terminal_status(before.assistant_text)
            if terminal:
                self._state = terminal
                return self._state

            delivery = page.send_simple_continue(
                CONTINUE_PROMPT,
                before.turn_key,
                acceptance_timeout=self.submission_confirm_seconds,
            )
            if not delivery.accepted:
                self._state = "submission_unknown" if delivery.uncertain else "send_rejected"
                return self._state

            polls = max(1, int(self.progress_window_seconds // self.progress_poll_seconds))
            for _ in range(polls):
                self.sleep(self.progress_poll_seconds)
                if not _same_conversation(page.current_url(), self.target_url):
                    self._state = "target_changed"
                    return self._state
                current = page.snapshot()
                self._completion_text = current.assistant_text
                if _visible_progress(before, current):
                    self._state = "progress_visible"
                    self._diagnostics = {"frontend_user_message_id": delivery.message_id}
                    return self._state

            self._state = "sent_no_visible_progress"
            self._diagnostics = {"frontend_user_message_id": delivery.message_id}
            return self._state
        finally:
            page.close()

    def close(self) -> None:
        # Each tick owns and closes its own transient Relay attachment.
        return None
