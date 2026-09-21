from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
import json
import time
from typing import Callable, Iterable, Mapping
from urllib.parse import urlparse
from urllib.request import urlopen

from .dom_snapshot import DOM_SNAPSHOT_JS
from .model import DomSignals, PageSnapshot, Phase, PromptDelivery, TurnKey, classify_phase
from .relay_cdp import RelayCdpError, RelayCdpProtocol


class RelayTargetSelectionError(RuntimeError):
    pass


def _build_retry_fault_expression() -> str:
    return r"""
(() => {
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };
  const retryPattern = /^(重试|retry|try again)$/i;
  const faultPattern = /(消息发送超时|消息流断开|message[^\n]{0,80}(?:timed out|timeout)|stream[^\n]{0,80}interrupted|response[^\n]{0,80}interrupted)/i;
  const buttons = Array.from(document.querySelectorAll('button')).filter((button) => {
    if (!visible(button)) return false;
    const label = (button.innerText || button.getAttribute('aria-label') || button.textContent || '').trim();
    return retryPattern.test(label) && !button.disabled && button.getAttribute('aria-disabled') !== 'true';
  });
  for (const button of buttons) {
    let node = button;
    for (let depth = 0; node && depth < 6; depth += 1, node = node.parentElement) {
      const value = (node.innerText || node.textContent || '').trim();
      if (faultPattern.test(value)) {
        button.click();
        return { retried: true };
      }
    }
  }
  return { retried: false, reason: 'fault-retry-unavailable' };
})()
""".strip()


def _build_submit_expression(
    prompt: str,
    *,
    allow_generation_active: bool = False,
    allow_user_turn_pending: bool = False,
) -> str:
    text = json.dumps(prompt)
    allow_active = "true" if allow_generation_active else "false"
    allow_pending = "true" if allow_user_turn_pending else "false"
    return f"""
(async () => {{
  const allowGenerationActive = {allow_active};
  const allowUserTurnPending = {allow_pending};
  const visible = (el) => {{
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  }};
  const assistants = Array.from(document.querySelectorAll('[data-message-author-role="assistant"]'));
  const users = Array.from(document.querySelectorAll('[data-message-author-role="user"]'));
  const latest = assistants.length ? assistants[assistants.length - 1] : null;
  const latestUser = users.length ? users[users.length - 1] : null;
  const userTurnPending = !!(latestUser && (
    !latest ||
    (typeof latest.compareDocumentPosition === 'function' &&
      (latest.compareDocumentPosition(latestUser) & 4) !== 0)
  ));
  const turn = latest
    ? (latest.closest('[data-testid^="conversation-turn-"]') || latest.closest('article[data-turn="assistant"]') || latest)
    : null;
  const stop = document.querySelector('[data-testid="stop-button"]');
  const busy = !!(turn && (turn.getAttribute('aria-busy') === 'true' || turn.querySelector('[aria-busy="true"]')));
  if (!allowGenerationActive && (visible(stop) || busy)) return {{ submitted: false, reason: 'generation-active' }};
  if (!allowUserTurnPending && userTurnPending) return {{ submitted: false, reason: 'user-turn-pending' }};

  const editor = document.querySelector('#prompt-textarea') ||
    document.querySelector('[contenteditable="true"][data-lexical-editor="true"]') ||
    document.querySelector('div[contenteditable="true"]');
  if (!editor || !visible(editor) || editor.getAttribute('aria-disabled') === 'true') {{
    return {{ submitted: false, reason: 'composer-unavailable' }};
  }}
  const existing = typeof editor.value === 'string'
    ? editor.value
    : (editor.innerText || editor.textContent || '');
  if (existing.trim()) return {{ submitted: false, reason: 'composer-not-empty' }};

  const text = {text};
  editor.focus();
  if (editor instanceof HTMLTextAreaElement || editor instanceof HTMLInputElement) {{
    const prototype = editor instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
    if (setter) setter.call(editor, text);
    else editor.value = text;
    editor.dispatchEvent(new InputEvent('input', {{ bubbles: true, inputType: 'insertText', data: text }}));
  }} else {{
    document.execCommand('selectAll', false);
    const inserted = document.execCommand('insertText', false, text);
    if (!inserted) {{
      editor.textContent = text;
      editor.dispatchEvent(new InputEvent('input', {{ bubbles: true, inputType: 'insertText', data: text }}));
    }}
  }}

  const findSend = () => document.querySelector('[data-testid="send-button"]') ||
    Array.from(document.querySelectorAll('button')).find((button) => {{
      const label = (button.getAttribute('aria-label') || button.textContent || '').trim().toLowerCase();
      return label === 'send' || label.includes('send message') || label.includes('发送');
    }});
  for (let attempt = 0; attempt < 16; attempt += 1) {{
    const button = findSend();
    if (button && visible(button) && !button.disabled && button.getAttribute('aria-disabled') !== 'true') {{
      button.click();
      return {{ submitted: true }};
    }}
    await new Promise((resolve) => setTimeout(resolve, 125));
  }}
  return {{ submitted: false, reason: 'send-unavailable' }};
}})()
""".strip()


@dataclass
class RelayChatGPTPage:
    target_id: str
    target_url: str
    session_id: str
    socket: object
    protocol: RelayCdpProtocol
    match_url: str
    relay_url: str | None = None
    fetch_json: Callable[[str], object] | None = None
    websocket_factory: object = None

    @classmethod
    def connect(
        cls,
        relay_url: str,
        match_url: str,
        *,
        fetch_json: Callable[[str], object] = None,
        websocket_factory=None,
    ) -> "RelayChatGPTPage":
        fetch = fetch_json or _fetch_json
        target = discover_unique_target(relay_url, match_url, fetch_json=fetch)
        target_id = str(target["id"])
        target_url = str(target["url"])
        ws_url = discover_websocket_url(relay_url, fetch_json=fetch)
        if websocket_factory is None:
            import websocket

            websocket_factory = websocket.create_connection
        socket = websocket_factory(ws_url, timeout=3.0, suppress_origin=True)
        protocol = RelayCdpProtocol(socket)
        try:
            session_id = protocol.attach_target(target_id)
        except BaseException:
            # Preserve the attach failure while releasing its unpublished transport.
            with suppress(Exception):
                socket.close()
            raise
        return cls(
            target_id=target_id,
            target_url=target_url,
            session_id=session_id,
            socket=socket,
            protocol=protocol,
            match_url=match_url,
            relay_url=relay_url,
            fetch_json=fetch,
            websocket_factory=websocket_factory,
        )

    def _reconnect(self) -> bool:
        if not self.relay_url or self.websocket_factory is None:
            return False
        replacement_socket = None
        try:
            target = discover_unique_target(
                self.relay_url,
                self.match_url,
                fetch_json=self.fetch_json or _fetch_json,
            )
            ws_url = discover_websocket_url(
                self.relay_url,
                fetch_json=self.fetch_json or _fetch_json,
            )
            replacement_socket = self.websocket_factory(
                ws_url,
                timeout=3.0,
                suppress_origin=True,
            )
            protocol = RelayCdpProtocol(replacement_socket)
            target_id = str(target["id"])
            session_id = protocol.attach_target(target_id)
        except Exception:
            close = getattr(replacement_socket, "close", None)
            if callable(close):
                close()
            return False

        # A stale socket cleanup failure must not discard a verified replacement.
        with suppress(Exception):
            self.close()
        self.target_id = target_id
        self.target_url = str(target["url"])
        self.session_id = session_id
        self.socket = replacement_socket
        self.protocol = protocol
        return True

    def snapshot(self, *, _allow_reconnect: bool = True) -> PageSnapshot:
        try:
            current_url = self.protocol.evaluate(self.session_id, "location.href")
            if (
                not isinstance(current_url, str)
                or not _is_chatgpt_url(current_url)
                or self.match_url not in current_url
            ):
                return PageSnapshot(
                    phase=Phase.BLOCKED,
                    assistant_turn_id="navigated-away",
                    assistant_text_signature="",
                    assistant_text="",
                    assistant_count=0,
                    user_count=0,
                )

            payload = self.protocol.evaluate(
                self.session_id,
                f"({DOM_SNAPSHOT_JS})()",
            )
        except RelayCdpError:
            if _allow_reconnect and self._reconnect():
                return self.snapshot(_allow_reconnect=False)
            return PageSnapshot(
                phase=Phase.BLOCKED,
                assistant_turn_id="relay-unavailable",
                assistant_text_signature="",
                assistant_text="",
                assistant_count=0,
                user_count=0,
            )
        if not isinstance(payload, Mapping):
            return PageSnapshot(
                phase=Phase.BLOCKED,
                assistant_turn_id="invalid-snapshot",
                assistant_text_signature="",
                assistant_text="",
                assistant_count=0,
                user_count=0,
            )
        signals = DomSignals(
            stop_visible=bool(payload.get("stopVisible")),
            assistant_busy=bool(payload.get("assistantBusy")),
            thinking_visible=bool(payload.get("thinkingVisible")),
            composer_ready=bool(payload.get("composerReady")),
            composer_has_draft=bool(payload.get("composerHasDraft", False)),
            assistant_present=bool(payload.get("assistantTurnId")),
            assistant_finalized=bool(payload.get("assistantFinalized", False)),
            user_turn_pending=bool(payload.get("userTurnPending", False)),
            interaction_required=bool(payload.get("interactionRequired", False)),
            send_timeout=bool(payload.get("sendTimeout", False)),
            stream_interrupted=bool(payload.get("streamInterrupted", False)),
        )
        return PageSnapshot(
            phase=classify_phase(signals),
            assistant_turn_id=str(payload.get("assistantTurnId", "")),
            assistant_text_signature=str(payload.get("assistantTextSignature", "")),
            assistant_text=str(payload.get("assistantText", "")),
            assistant_count=int(payload.get("assistantCount", 0)),
            user_count=int(payload.get("userCount", 0)),
            user_turn_id=str(payload.get("userTurnId", "")),
            user_text=str(payload.get("userText", "")),
            stop_visible=bool(payload.get("stopVisible", False)),
            composer_has_draft=bool(payload.get("composerHasDraft", False)),
            submission_seq=int(payload.get("submissionSeq", 0)),
            submission_receipt_seq=int(payload.get("submissionReceiptSeq", 0)),
            submission_receipt_id=str(payload.get("submissionReceiptId", "")),
            submission_receipt_text=str(payload.get("submissionReceiptText", "")),
            trusted_submission_receipt_seq=int(payload.get("trustedSubmissionReceiptSeq", 0)),
            trusted_submission_receipt_id=str(payload.get("trustedSubmissionReceiptId", "")),
            user_turn_pending=bool(payload.get("userTurnPending", False)),
            interaction_required=bool(payload.get("interactionRequired", False)),
            send_timeout=bool(payload.get("sendTimeout", False)),
            stream_interrupted=bool(payload.get("streamInterrupted", False)),
            fault_text=str(payload.get("faultText", "")),
        )

    def _send_prompt(
        self,
        prompt: str,
        expected_turn_key: TurnKey,
        *,
        acceptance_timeout: float,
        require_message_id: bool,
        allow_blocked: bool = False,
        allow_active: bool = False,
        allow_user_turn_pending: bool = False,
        require_frontend_acceptance: bool = False,
    ) -> PromptDelivery:
        before = self.snapshot()
        if before.turn_key != expected_turn_key:
            # The caller's precondition changed before we performed an effect.
            return PromptDelivery(accepted=False, stale=True)
        if (
            before.phase is not Phase.FINISHED
            and not (allow_blocked and before.phase is Phase.BLOCKED)
            and not (
                allow_active
                and before.phase in (Phase.THINKING, Phase.RESPONDING)
            )
        ):
            # No effect was attempted. If the page is already active, the
            # caller's blocked/finished precondition has simply gone stale;
            # never report this as acceptance of the prompt we did not send.
            return PromptDelivery(
                accepted=False,
                stale=before.phase in (Phase.THINKING, Phase.RESPONDING),
            )

        effect_unknown = False
        try:
            result = self.protocol.evaluate(
                self.session_id,
                _build_submit_expression(
                    prompt,
                    allow_generation_active=allow_active,
                    allow_user_turn_pending=allow_user_turn_pending,
                ),
                await_promise=True,
            )
        except RelayCdpError:
            # The transport can fail after the browser effect. Reconcile the
            # causal receipt before deciding; never translate this into a safe
            # rejection that an upper layer could retry.
            result = None
            effect_unknown = True
        if isinstance(result, Mapping) and result.get("submitted") is not True:
            return PromptDelivery(accepted=False)
        if not isinstance(result, Mapping):
            effect_unknown = True

        expected_text = prompt.replace("\r\n", "\n").strip()
        baseline_receipt_seq = before.submission_receipt_seq
        deadline = time.monotonic() + acceptance_timeout
        while time.monotonic() < deadline:
            current = self.snapshot()
            receipt_text = current.submission_receipt_text.replace("\r\n", "\n").strip()
            frontend_accepted = (
                current.user_text.replace("\r\n", "\n").strip() == expected_text
                and not current.composer_has_draft
                and current.stop_visible
            )
            if (
                current.submission_receipt_seq > baseline_receipt_seq
                and current.submission_receipt_id
                and receipt_text == expected_text
                and (not require_frontend_acceptance or frontend_accepted)
            ):
                return PromptDelivery(
                    accepted=True,
                    message_id=current.submission_receipt_id,
                )
            time.sleep(0.1)
        # We know a submit may have happened (explicit submitted=true or a
        # transport failure with unknown effects), but no causal receipt bound
        # it to this prompt. Freeze retries and surface uncertainty.
        return PromptDelivery(accepted=False, uncertain=True)

    def send_prompt(
        self,
        prompt: str,
        expected_turn_key: TurnKey,
        acceptance_timeout: float = 3.0,
    ) -> PromptDelivery:
        return self._send_prompt(
            prompt,
            expected_turn_key,
            acceptance_timeout=acceptance_timeout,
            require_message_id=True,
        )

    def send_continue(
        self,
        prompt: str,
        expected_turn_key: TurnKey,
        acceptance_timeout: float = 3.0,
    ) -> PromptDelivery:
        return self._send_prompt(
            prompt,
            expected_turn_key,
            acceptance_timeout=acceptance_timeout,
            require_message_id=False,
            allow_blocked=True,
        )

    def send_simple_continue(
        self,
        prompt: str,
        expected_turn_key: TurnKey,
        acceptance_timeout: float = 40.0,
    ) -> PromptDelivery:
        return self._send_prompt(
            prompt,
            expected_turn_key,
            acceptance_timeout=acceptance_timeout,
            require_message_id=False,
            allow_blocked=True,
            allow_active=True,
            allow_user_turn_pending=True,
            require_frontend_acceptance=True,
        )

    def send_liveness_continue(
        self,
        prompt: str,
        expected_turn_key: TurnKey,
        acceptance_timeout: float = 3.0,
    ) -> PromptDelivery:
        """Send a continuation after verified visible-output liveness timeout.

        Unlike ordinary continuation delivery, this path may submit while the
        DOM still reports THINKING/RESPONDING. It is intentionally exposed only
        to the supervisor's one-shot 360s liveness recovery path.
        """
        return self._send_prompt(
            prompt,
            expected_turn_key,
            acceptance_timeout=acceptance_timeout,
            require_message_id=False,
            allow_blocked=True,
            allow_active=True,
        )

    def refresh(self) -> None:
        self.protocol.reload_page(self.session_id)

    def current_url(self) -> str:
        try:
            value = self.protocol.evaluate(self.session_id, "location.href")
        except RelayCdpError:
            if not self._reconnect():
                raise
            value = self.protocol.evaluate(self.session_id, "location.href")
        return str(value or "")

    def retry_fault(
        self,
        expected_turn_key: TurnKey,
        acceptance_timeout: float = 3.0,
    ) -> PromptDelivery:
        before = self.snapshot()
        if before.turn_key != expected_turn_key:
            return PromptDelivery(accepted=False, stale=True)
        if not before.send_timeout and not before.stream_interrupted:
            return PromptDelivery(
                accepted=before.phase in (Phase.THINKING, Phase.RESPONDING)
            )
        try:
            result = self.protocol.evaluate(
                self.session_id,
                _build_retry_fault_expression(),
            )
        except RelayCdpError:
            return PromptDelivery(accepted=False, uncertain=True)
        if not isinstance(result, Mapping):
            return PromptDelivery(accepted=False, uncertain=True)
        if result.get("retried") is not True:
            return PromptDelivery(accepted=False)

        deadline = time.monotonic() + acceptance_timeout
        while time.monotonic() < deadline:
            current = self.snapshot()
            if current.phase in (Phase.THINKING, Phase.RESPONDING):
                return PromptDelivery(accepted=True)
            time.sleep(0.1)
        return PromptDelivery(accepted=False, uncertain=True)

    def close(self) -> None:
        close = getattr(self.socket, "close", None)
        if callable(close):
            close()


def _is_chatgpt_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host == "chatgpt.com" or host.endswith(".chatgpt.com")


def _fetch_json(url: str):
    with urlopen(url, timeout=3.0) as response:
        return json.loads(response.read().decode("utf-8"))


def discover_websocket_url(
    relay_url: str,
    *,
    fetch_json: Callable[[str], object] = _fetch_json,
) -> str:
    payload = fetch_json(f"{relay_url.rstrip('/')}/json/version")
    if not isinstance(payload, Mapping):
        raise RelayTargetSelectionError("relay /json/version returned a non-object payload")
    ws_url = payload.get("webSocketDebuggerUrl")
    if not isinstance(ws_url, str) or not ws_url:
        raise RelayTargetSelectionError("relay /json/version did not provide webSocketDebuggerUrl")
    return ws_url


def discover_unique_target(
    relay_url: str,
    match_url: str,
    *,
    fetch_json: Callable[[str], object] = _fetch_json,
) -> Mapping[str, object]:
    targets = fetch_json(f"{relay_url.rstrip('/')}/json/list".replace("\\/", "/"))
    if not isinstance(targets, list):
        raise RelayTargetSelectionError("relay /json/list returned a non-list payload")
    return select_unique_target(targets, match_url)


def select_unique_target(
    targets: Iterable[Mapping[str, object]],
    match_url: str,
) -> Mapping[str, object]:
    matches = [
        target
        for target in targets
        if target.get("type") == "page"
        and isinstance(target.get("url"), str)
        and _is_chatgpt_url(str(target["url"]))
        and match_url in str(target["url"])
    ]
    if not matches:
        raise RelayTargetSelectionError(
            f"no matching ChatGPT target for URL substring {match_url!r}"
        )
    if len(matches) > 1:
        urls = ", ".join(str(target.get("url", "")) for target in matches)
        raise RelayTargetSelectionError(
            f"multiple matching ChatGPT targets; use a more specific --match-url: {urls}"
        )
    return matches[0]
