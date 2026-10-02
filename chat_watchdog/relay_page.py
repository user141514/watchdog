from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
import hashlib
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


TEMPORARY_CHAT_STATE_JS = r"""
(() => {
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none'
      && style.visibility !== 'hidden'
      && rect.width > 0
      && rect.height > 0;
  };
  const labels = new Set(['临时聊天', 'Temporary Chat']);
  const button = Array.from(document.querySelectorAll('button')).find((el) =>
    labels.has((el.getAttribute('aria-label') || '').trim())
  ) || null;
  const editor = document.querySelector('[contenteditable="true"][role="textbox"]')
    || document.querySelector('#prompt-textarea')
    || document.querySelector('[contenteditable="true"][data-lexical-editor="true"]')
    || document.querySelector('div[contenteditable="true"]');
  let isTemporaryChat = null;
  try {
    if (new URL(location.href).searchParams.get('temporary-chat') === 'true') {
      isTemporaryChat = true;
    }
  } catch (_) {}
  if (button) {
    const fiberKey = Object.keys(button).find((key) => key.startsWith('__reactFiber$'));
    let fiber = fiberKey ? button[fiberKey] : null;
    for (let depth = 0; fiber && depth < 40; depth += 1, fiber = fiber.return) {
      const props = fiber.memoizedProps;
      if (
        props
        && typeof props === 'object'
        && typeof props.isTemporaryChat === 'boolean'
        && isTemporaryChat !== true
      ) {
        isTemporaryChat = props.isTemporaryChat;
      }
    }
  }
  return {
    href: location.href,
    buttonFound: !!button,
    buttonVisible: visible(button),
    buttonDisabled: !!button && (
      button.disabled === true || button.getAttribute('aria-disabled') === 'true'
    ),
    composerReady: visible(editor),
    isTemporaryChat,
  };
})()
""".strip()


PASTE_STAGED_COMPOSER_JS = r"""
(() => {
  const editor = document.querySelector('[contenteditable="true"][role="textbox"]')
    || document.querySelector('#prompt-textarea')
    || document.querySelector('[contenteditable="true"][data-lexical-editor="true"]')
    || document.querySelector('div[contenteditable="true"]');
  if (!editor) return { pasted: false, reason: 'composer-missing' };
  const text = (globalThis.__watchdogPromptChunks || []).join('');
  editor.focus();
  const transfer = new DataTransfer();
  transfer.setData('text/plain', text);
  const event = new ClipboardEvent('paste', {
    bubbles: true,
    cancelable: true,
    clipboardData: transfer,
  });
  editor.dispatchEvent(event);
  return {
    pasted: event.defaultPrevented === true,
    chars: text.length,
  };
})()
""".strip()


COMPOSER_FINGERPRINT_JS = r"""
(async () => {
  const editor = document.querySelector('[contenteditable="true"][role="textbox"]')
    || document.querySelector('#prompt-textarea')
    || document.querySelector('[contenteditable="true"][data-lexical-editor="true"]')
    || document.querySelector('div[contenteditable="true"]');
  if (!editor) return { ok: false, reason: 'composer-missing' };
  const literalSpans = Array.from(
    editor.querySelectorAll('[data-prompt-literal-paste]')
  );
  const serializeLiteral = (node) => {
    if (node.nodeType === Node.TEXT_NODE) return node.nodeValue || '';
    if (node.nodeType !== Node.ELEMENT_NODE) return '';
    if (node.tagName === 'BR') return '\n';
    return Array.from(node.childNodes).map(serializeLiteral).join('');
  };
  const text = literalSpans.length
    ? literalSpans.map(serializeLiteral).join('')
    : (editor.innerText || editor.textContent || '');
  const bytes = new TextEncoder().encode(text);
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  const sha256 = Array.from(new Uint8Array(digest))
    .map((value) => value.toString(16).padStart(2, '0'))
    .join('');
  const form = editor.closest?.('form') || null;
  const send = form?.querySelector?.('button[type="submit"]') || null;
  return {
    ok: true,
    chars: text.length,
    sha256,
    sendReady: !!send
      && !send.disabled
      && send.getAttribute('aria-disabled') !== 'true',
  };
})()
""".strip()


SUBMIT_STAGED_COMPOSER_JS = r"""
(() => {
  const editor = document.querySelector('[contenteditable="true"][role="textbox"]')
    || document.querySelector('#prompt-textarea')
    || document.querySelector('[contenteditable="true"][data-lexical-editor="true"]')
    || document.querySelector('div[contenteditable="true"]');
  const form = editor?.closest?.('form') || null;
  const button = form?.querySelector?.('button[type="submit"]') || null;
  if (!editor || !form || !button) {
    return { submitted: false, reason: 'send-unavailable' };
  }
  if (button.disabled || button.getAttribute('aria-disabled') === 'true') {
    return { submitted: false, reason: 'send-disabled' };
  }
  button.click();
  return { submitted: true };
})()
""".strip()


ENABLE_TEMPORARY_CHAT_JS = r"""
(() => {
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none'
      && style.visibility !== 'hidden'
      && rect.width > 0
      && rect.height > 0;
  };
  const labels = new Set(['临时聊天', 'Temporary Chat']);
  const button = Array.from(document.querySelectorAll('button')).find((el) =>
    labels.has((el.getAttribute('aria-label') || '').trim())
  ) || null;
  if (!button) return {clicked: false, reason: 'temporary-chat-button-missing'};
  if (!visible(button)) return {clicked: false, reason: 'temporary-chat-button-hidden'};
  if (button.disabled || button.getAttribute('aria-disabled') === 'true') {
    return {clicked: false, reason: 'temporary-chat-button-disabled'};
  }

  let isTemporaryChat = null;
  const fiberKey = Object.keys(button).find((key) => key.startsWith('__reactFiber$'));
  let fiber = fiberKey ? button[fiberKey] : null;
  for (let depth = 0; fiber && depth < 40; depth += 1, fiber = fiber.return) {
    const props = fiber.memoizedProps;
    if (
      props
      && typeof props === 'object'
      && typeof props.isTemporaryChat === 'boolean'
    ) {
      isTemporaryChat = props.isTemporaryChat;
    }
  }
  if (isTemporaryChat === true) {
    return {clicked: false, alreadyTemporary: true};
  }
  button.click();
  return {clicked: true, alreadyTemporary: false};
})()
""".strip()


def _build_stage_composer_expression(prompt: str) -> str:
    text = json.dumps(prompt)
    return f"""
(() => {{
  const visible = (el) => {{
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none'
      && style.visibility !== 'hidden'
      && rect.width > 0
      && rect.height > 0;
  }};
  const editor = document.querySelector('[contenteditable="true"][role="textbox"]')
    || document.querySelector('#prompt-textarea')
    || document.querySelector('[contenteditable="true"][data-lexical-editor="true"]')
    || document.querySelector('div[contenteditable="true"]');
  if (!editor || !visible(editor) || editor.getAttribute('aria-disabled') === 'true') {{
    return {{ staged: false, reason: 'composer-unavailable' }};
  }}

  const text = {text};
  const existing = typeof editor.value === 'string'
    ? editor.value
    : (editor.innerText || editor.textContent || '');
  const normalizedExisting = existing.replace(/\\r\\n/g, '\\n').trim();
  const normalizedText = text.replace(/\\r\\n/g, '\\n').trim();
  if (normalizedExisting && normalizedExisting !== normalizedText) {{
    return {{ staged: false, reason: 'composer-not-empty' }};
  }}
  if (normalizedExisting === normalizedText) {{
    editor.focus();
    return {{ staged: true, alreadyStaged: true, text: normalizedExisting }};
  }}

  editor.focus();
  if (editor instanceof HTMLTextAreaElement || editor instanceof HTMLInputElement) {{
    const prototype = editor instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
    if (setter) setter.call(editor, text);
    else editor.value = text;
    editor.dispatchEvent(new InputEvent(
      'input',
      {{ bubbles: true, inputType: 'insertText', data: text }}
    ));
  }} else {{
    document.execCommand('selectAll', false);
    const inserted = document.execCommand('insertText', false, text);
    if (!inserted) {{
      editor.textContent = text;
      editor.dispatchEvent(new InputEvent(
        'input',
        {{ bubbles: true, inputType: 'insertText', data: text }}
      ));
    }}
  }}
  const actual = typeof editor.value === 'string'
    ? editor.value
    : (editor.innerText || editor.textContent || '');
  const normalizedActual = actual.replace(/\\r\\n/g, '\\n').trim();
  return {{
    staged: normalizedActual === normalizedText,
    alreadyStaged: false,
    text: normalizedActual,
  }};
}})()
""".strip()


def _build_clear_staged_composer_expression(prompt: str) -> str:
    text = json.dumps(prompt)
    return f"""
(() => {{
  const editor = document.querySelector('[contenteditable="true"][role="textbox"]')
    || document.querySelector('#prompt-textarea')
    || document.querySelector('[contenteditable="true"][data-lexical-editor="true"]')
    || document.querySelector('div[contenteditable="true"]');
  if (!editor) return {{ cleared: false, reason: 'composer-unavailable' }};
  const expected = {text}.replace(/\\r\\n/g, '\\n').trim();
  const actual = (typeof editor.value === 'string'
    ? editor.value
    : (editor.innerText || editor.textContent || '')
  ).replace(/\\r\\n/g, '\\n').trim();
  if (actual !== expected) {{
    return {{ cleared: false, reason: 'composer-changed', text: actual }};
  }}
  editor.focus();
  document.execCommand('selectAll', false);
  document.execCommand('delete', false);
  if (typeof editor.value === 'string' && editor.value) {{
    const prototype = editor instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
    if (setter) setter.call(editor, '');
    else editor.value = '';
    editor.dispatchEvent(new InputEvent(
      'input',
      {{ bubbles: true, inputType: 'deleteContentBackward', data: null }}
    ));
  }}
  return {{ cleared: true }};
}})()
""".strip()


RECOVERY_SETTLE_STATE_JS = r"""
(() => {
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none'
      && style.visibility !== 'hidden'
      && rect.width > 0
      && rect.height > 0;
  };

  const editor = document.querySelector('[contenteditable="true"][role="textbox"]')
    || document.querySelector('#prompt-textarea')
    || document.querySelector('[contenteditable="true"][data-lexical-editor="true"]')
    || document.querySelector('div[contenteditable="true"]');
  const form = editor?.closest?.('form') || null;
  const send = form?.querySelector?.('button[type="submit"]') || null;
  const stop = document.querySelector('[data-testid="stop-button"]')
    || Array.from(document.querySelectorAll('button')).find((button) => {
      const label = (button.getAttribute('aria-label') || button.textContent || '').trim().toLowerCase();
      return label === 'stop' || label === '停止';
    }) || null;

  const latest = Array.from(
    document.querySelectorAll('[class*="MarkdownRoot-"]')
  ).at(-1) || null;

  const state = {
    turnId: '',
    completed: null,
    isStreaming: null,
    wasStopped: null,
  };
  if (latest) {
    const fiberKey = Object.keys(latest).find((key) => key.startsWith('__reactFiber$'));
    let fiber = fiberKey ? latest[fiberKey] : null;
    for (let depth = 0; fiber && depth < 60; depth += 1, fiber = fiber.return) {
      const props = fiber.memoizedProps;
      if (!props || typeof props !== 'object') continue;
      if (typeof props.turnId === 'string' && props.turnId) state.turnId = props.turnId;
      for (const key of ['completed', 'isStreaming', 'wasStopped']) {
        if (Object.prototype.hasOwnProperty.call(props, key)) {
          state[key] = props[key];
        }
      }
    }
  }

  const composerText = editor
    ? (typeof editor.value === 'string'
      ? editor.value
      : (editor.innerText || editor.textContent || ''))
    : '';

  return {
    stopVisible: visible(stop),
    sendReady: !!send
      && visible(send)
      && !send.disabled
      && send.getAttribute('aria-disabled') !== 'true',
    composerReady: visible(editor),
    composerText,
    assistantTurnId: state.turnId,
    completed: state.completed,
    isStreaming: state.isStreaming,
    wasStopped: state.wasStopped,
  };
})()
""".strip()


FOCUS_COMPOSER_JS = r"""
(() => {
  const editor = document.querySelector('[contenteditable="true"][role="textbox"]')
    || document.querySelector('#prompt-textarea')
    || document.querySelector('[contenteditable="true"][data-lexical-editor="true"]')
    || document.querySelector('div[contenteditable="true"]');
  if (!editor) return { focused: false, reason: 'composer-unavailable' };
  editor.focus();
  return {
    focused: document.activeElement === editor || editor.contains(document.activeElement),
    text: typeof editor.value === 'string'
      ? editor.value
      : (editor.innerText || editor.textContent || ''),
  };
})()
""".strip()


def _build_stop_generation_expression() -> str:
    return r"""
(() => {
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none'
      && style.visibility !== 'hidden'
      && rect.width > 0
      && rect.height > 0;
  };
  const stop = document.querySelector('[data-testid="stop-button"]')
    || Array.from(document.querySelectorAll('button')).find((button) => {
      const label = (button.getAttribute('aria-label') || button.textContent || '').trim().toLowerCase();
      return label === 'stop' || label === '停止';
    });
  if (!stop) return { stopped: false, reason: 'stop-unavailable' };
  if (!visible(stop)) return { stopped: false, reason: 'stop-hidden' };
  if (stop.disabled || stop.getAttribute('aria-disabled') === 'true') {
    return { stopped: false, reason: 'stop-disabled' };
  }
  stop.click();
  return { stopped: true };
})()
""".strip()


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
  const reactPropChain = (el, maxDepth = 60) => {{
    if (!el) return [];
    const fiberKey = Object.keys(el).find((key) => key.startsWith('__reactFiber$'));
    let fiber = fiberKey ? el[fiberKey] : null;
    const chain = [];
    for (let depth = 0; fiber && depth < maxDepth; depth += 1, fiber = fiber.return) {{
      const props = fiber.memoizedProps;
      if (props && typeof props === 'object') chain.push(props);
    }}
    return chain;
  }};
  const userProps = (el) => {{
    const state = {{messageId: '', turnId: ''}};
    for (const props of reactPropChain(el)) {{
      if (typeof props.messageId === 'string' && props.messageId) state.messageId = props.messageId;
      if (typeof props.turnId === 'string' && props.turnId) state.turnId = props.turnId;
    }}
    return state.messageId || state.turnId ? state : null;
  }};
  const assistantProps = (el) => {{
    const state = {{turnId: '', completed: null, isStreaming: null}};
    let found = false;
    for (const props of reactPropChain(el)) {{
      if (typeof props.turnId === 'string' && props.turnId) {{
        state.turnId = props.turnId;
        found = true;
      }}
      if (Object.prototype.hasOwnProperty.call(props, 'completed')) {{
        state.completed = props.completed;
        found = true;
      }}
      if (Object.prototype.hasOwnProperty.call(props, 'isStreaming')) {{
        state.isStreaming = props.isStreaming;
        found = true;
      }}
    }}
    return found ? state : null;
  }};
  let assistants = Array.from(document.querySelectorAll('[data-message-author-role="assistant"]'));
  if (!assistants.length) assistants = Array.from(document.querySelectorAll('[class*="MarkdownRoot-"]'));
  let users = Array.from(document.querySelectorAll('[data-message-author-role="user"]'));
  if (!users.length) users = Array.from(document.querySelectorAll('.bg-user-message'));
  const latest = assistants.length ? assistants[assistants.length - 1] : null;
  const latestUser = users.length ? users[users.length - 1] : null;
  const latestAssistantProps = assistantProps(latest);
  const latestUserProps = userProps(latestUser);
  const assistantTurnId = String(latestAssistantProps?.turnId || '');
  const userTurnId = String(latestUserProps?.turnId || '');
  const userTurnPending = (assistantTurnId || userTurnId)
    ? !!(latestUser && (!latest || !assistantTurnId || !userTurnId || assistantTurnId !== userTurnId))
    : !!(latestUser && (
        !latest ||
        (typeof latest.compareDocumentPosition === 'function' &&
          (latest.compareDocumentPosition(latestUser) & 4) !== 0)
      ));
  const turn = latest
    ? (latest.closest('[data-testid^="conversation-turn-"]') || latest.closest('article[data-turn="assistant"]') || latest)
    : null;
  const stop = document.querySelector('[data-testid="stop-button"]') ||
    Array.from(document.querySelectorAll('button')).find((button) => {{
      const label = (button.getAttribute('aria-label') || button.textContent || '').trim().toLowerCase();
      return label === 'stop' || label === '停止';
    }});
  const busy = !!((turn && (turn.getAttribute('aria-busy') === 'true' || turn.querySelector('[aria-busy="true"]'))) ||
    (latestAssistantProps && (
      latestAssistantProps.isStreaming === true || latestAssistantProps.completed === false
    )));
  if (!allowGenerationActive && (visible(stop) || busy)) return {{ submitted: false, reason: 'generation-active' }};
  if (!allowUserTurnPending && userTurnPending) return {{ submitted: false, reason: 'user-turn-pending' }};

  const editor = document.querySelector('#prompt-textarea') ||
    document.querySelector('[contenteditable="true"][data-lexical-editor="true"]') ||
    document.querySelector('[contenteditable="true"][role="textbox"]') ||
    document.querySelector('div[contenteditable="true"]');
  if (!editor || !visible(editor) || editor.getAttribute('aria-disabled') === 'true') {{
    return {{ submitted: false, reason: 'composer-unavailable' }};
  }}
  const text = {text};
  const existing = typeof editor.value === 'string'
    ? editor.value
    : (editor.innerText || editor.textContent || '');
  const normalizedExisting = existing.replace(/\\r\\n/g, '\\n').trim();
  const normalizedText = text.replace(/\\r\\n/g, '\\n').trim();
  if (normalizedExisting && normalizedExisting !== normalizedText) {{
    return {{ submitted: false, reason: 'composer-not-empty' }};
  }}

  if (!normalizedExisting) {{
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
  }}

  const form = editor.closest?.('form') || null;
  const findSend = () => form?.querySelector?.('button[type="submit"]') ||
    document.querySelector('[data-testid="send-button"]') ||
    Array.from(document.querySelectorAll('button')).find((button) => {{
      const label = (button.getAttribute('aria-label') || button.textContent || '').trim().toLowerCase();
      return label === 'send' || label.includes('send message') || label.includes('发送');
    }});
  for (let attempt = 0; attempt < 16; attempt += 1) {{
    const button = findSend();
    if (button && visible(button) && !button.disabled && button.getAttribute('aria-disabled') !== 'true') {{
      // ChatGPT's current ProseMirror composer wires send behavior to the
      // button interaction itself; native requestSubmit() can leave the draft
      // untouched. Keep selection scoped to the composer form, then click.
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
    def open_temporary_chat(
        cls,
        relay_url: str,
        *,
        timeout: float = 10.0,
        poll_interval: float = 0.1,
        request_timeout: float = 30.0,
        home_url: str = "https://chatgpt.com/",
        fetch_json: Callable[[str], object] = None,
        websocket_factory=None,
    ) -> "RelayChatGPTPage":
        fetch = fetch_json or _fetch_json
        ws_url = discover_websocket_url(relay_url, fetch_json=fetch)
        if websocket_factory is None:
            import websocket

            websocket_factory = websocket.create_connection
        socket = websocket_factory(ws_url, timeout=3.0, suppress_origin=True)
        protocol = RelayCdpProtocol(socket, request_timeout=request_timeout)
        target_id = ""
        try:
            target_id = protocol.create_target(home_url)
            session_id = protocol.attach_target(target_id)
            page = cls(
                target_id=target_id,
                target_url=home_url,
                session_id=session_id,
                socket=socket,
                protocol=protocol,
                match_url="https://chatgpt.com/",
                relay_url=relay_url,
                fetch_json=fetch,
                websocket_factory=websocket_factory,
            )
            page.enable_temporary_chat(
                timeout=timeout,
                poll_interval=poll_interval,
            )
            return page
        except BaseException:
            if target_id:
                with suppress(Exception):
                    protocol.command("Target.closeTarget", {"targetId": target_id})
            with suppress(Exception):
                socket.close()
            raise

    @classmethod
    def open_isolated_judge_chat(
        cls,
        relay_url: str,
        **kwargs,
    ) -> "RelayChatGPTPage":
        """Open a fresh Temporary Chat suitable for context-isolated judging."""
        return cls.open_temporary_chat(relay_url, **kwargs)

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
        explicit_submit = isinstance(result, Mapping) and result.get("submitted") is True
        deadline = time.monotonic() + acceptance_timeout
        while time.monotonic() < deadline:
            current = self.snapshot()
            receipt_text = current.submission_receipt_text.replace("\r\n", "\n").strip()
            frontend_accepted = (
                explicit_submit
                and bool(current.user_turn_id)
                and current.user_turn_id != before.user_turn_id
                and current.user_text.replace("\r\n", "\n").strip() == expected_text
                and not current.composer_has_draft
            )
            if require_frontend_acceptance and frontend_accepted:
                return PromptDelivery(
                    accepted=True,
                    message_id=current.user_turn_id,
                )
            if (
                not require_frontend_acceptance
                and current.submission_receipt_seq > baseline_receipt_seq
                and current.submission_receipt_id
                and receipt_text == expected_text
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

    def recover_stalled_active(
        self,
        prompt: str,
        expected_turn_key: TurnKey,
        *,
        stop_timeout: float = 10.0,
        acceptance_timeout: float = 40.0,
    ) -> PromptDelivery:
        """Recover one verified-stalled active turn using the current UI.

        Current ChatGPT behavior is:
        - the composer remains editable while generation is active;
        - pressing Enter while generation is active does NOT submit;
        - after Stop settles, focusing that staged draft and pressing Enter DOES
          submit a new user turn.

        So the recovery sequence is:
        stage exact continuation -> Stop -> wait settled -> focus -> Enter ->
        verify a new user turn carrying the exact staged text.
        """

        before = self.snapshot()
        if before.turn_key != expected_turn_key:
            return PromptDelivery(accepted=False, stale=True)
        if before.phase not in (Phase.THINKING, Phase.RESPONDING):
            return PromptDelivery(accepted=False, stale=True)
        if not before.stop_visible:
            return PromptDelivery(accepted=False)

        # Stage only into an empty composer. If the user already has a draft,
        # never overwrite it.
        try:
            staged = self.protocol.evaluate(
                self.session_id,
                _build_stage_composer_expression(prompt),
            )
        except RelayCdpError:
            return PromptDelivery(accepted=False, uncertain=True)
        if not isinstance(staged, Mapping):
            return PromptDelivery(accepted=False, uncertain=True)
        if staged.get("staged") is not True:
            return PromptDelivery(accepted=False)

        def clear_our_draft() -> None:
            with suppress(Exception):
                self.protocol.evaluate(
                    self.session_id,
                    _build_clear_staged_composer_expression(prompt),
                )

        try:
            stopped = self.protocol.evaluate(
                self.session_id,
                _build_stop_generation_expression(),
            )
        except RelayCdpError:
            clear_our_draft()
            return PromptDelivery(accepted=False, uncertain=True)
        if not isinstance(stopped, Mapping):
            clear_our_draft()
            return PromptDelivery(accepted=False, uncertain=True)
        if stopped.get("stopped") is not True:
            clear_our_draft()
            return PromptDelivery(accepted=False)

        expected = prompt.replace("\r\n", "\n").strip()
        before_user_text = before.user_text.replace("\r\n", "\n").strip()
        before_user_count = before.user_count
        deadline = time.monotonic() + stop_timeout
        settled: PageSnapshot | None = None
        while time.monotonic() < deadline:
            current = self.snapshot()
            current_user_text = current.user_text.replace("\r\n", "\n").strip()
            # Current ChatGPT may remap the same visible user message from a
            # local UUID to a server UUID while Stop settles. Treat only a
            # semantic message change as a human/new-caller race.
            if (
                current.user_count > before_user_count
                or (
                    current_user_text
                    and before_user_text
                    and current_user_text != before_user_text
                )
            ):
                clear_our_draft()
                return PromptDelivery(accepted=False, stale=True)
            try:
                ui = self.protocol.evaluate(
                    self.session_id,
                    RECOVERY_SETTLE_STATE_JS,
                )
            except RelayCdpError:
                clear_our_draft()
                return PromptDelivery(accepted=False, uncertain=True)
            if not isinstance(ui, Mapping):
                clear_our_draft()
                return PromptDelivery(accepted=False, uncertain=True)

            ui_text = str(ui.get("composerText") or "").replace("\r\n", "\n").strip()
            if ui_text and ui_text != expected:
                # User/UI changed the staged draft while Stop was settling.
                return PromptDelivery(accepted=False, stale=True)

            ui_turn = str(ui.get("assistantTurnId") or "")
            if (
                ui_turn
                and before.assistant_turn_id
                and ui_turn != before.assistant_turn_id
            ):
                return PromptDelivery(accepted=False, stale=True)

            if (
                ui.get("stopVisible") is False
                and ui.get("sendReady") is True
                and ui.get("composerReady") is True
                and ui.get("completed") is True
                and ui.get("isStreaming") is False
                and not current.user_turn_pending
            ):
                settled = current
                break
            time.sleep(0.1)

        if settled is None:
            clear_our_draft()
            return PromptDelivery(accepted=False, uncertain=True)

        # Verify the exact watchdog draft survived semantic Stop finality and
        # focus the composer before issuing a real Enter keypress.
        try:
            focused = self.protocol.evaluate(
                self.session_id,
                FOCUS_COMPOSER_JS,
            )
        except RelayCdpError:
            clear_our_draft()
            return PromptDelivery(accepted=False, uncertain=True)
        if not isinstance(focused, Mapping) or focused.get("focused") is not True:
            clear_our_draft()
            return PromptDelivery(accepted=False)
        actual = str(focused.get("text") or "").replace("\r\n", "\n").strip()
        if actual != expected:
            # The draft changed while we were stopping; assume human/UI race and
            # do not press Enter.
            return PromptDelivery(accepted=False, stale=True)

        # Real keyboard Enter, not a synthetic DOM key event and not a Send click.
        try:
            self.protocol.command(
                "Input.dispatchKeyEvent",
                {
                    "type": "keyDown",
                    "key": "Enter",
                    "code": "Enter",
                    "windowsVirtualKeyCode": 13,
                    "nativeVirtualKeyCode": 13,
                },
                session_id=self.session_id,
            )
            self.protocol.command(
                "Input.dispatchKeyEvent",
                {
                    "type": "keyUp",
                    "key": "Enter",
                    "code": "Enter",
                    "windowsVirtualKeyCode": 13,
                    "nativeVirtualKeyCode": 13,
                },
                session_id=self.session_id,
            )
        except RelayCdpError:
            # Enter may have taken effect before transport failure; freeze retry.
            return PromptDelivery(accepted=False, uncertain=True)

        settled_user_text = settled.user_text.replace("\r\n", "\n").strip()
        settled_user_count = settled.user_count
        deadline = time.monotonic() + acceptance_timeout
        while time.monotonic() < deadline:
            current = self.snapshot()
            current_text = current.user_text.replace("\r\n", "\n").strip()
            is_new_user_message = (
                current.user_count > settled_user_count
                or current.user_turn_id != settled.user_turn_id
            )
            if is_new_user_message:
                if current_text == expected and not current.composer_has_draft:
                    return PromptDelivery(
                        accepted=True,
                        message_id=current.user_turn_id,
                    )
                # Ignore pure UUID remaps of the same pre-existing user message.
                if current_text == settled_user_text:
                    time.sleep(0.1)
                    continue
                if current_text and current_text != expected:
                    return PromptDelivery(accepted=False, stale=True)
            time.sleep(0.1)

        # Enter was attempted but no causal user-turn receipt was observed.
        return PromptDelivery(accepted=False, uncertain=True)

    def send_liveness_continue(
        self,
        prompt: str,
        expected_turn_key: TurnKey,
        acceptance_timeout: float = 3.0,
    ) -> PromptDelivery:
        """Legacy direct-active submission path.

        Kept for API compatibility with external callers/tests. SimpleWatcher
        no longer uses this path because the current ChatGPT UI does not expose
        a send button while generation is active.
        """
        return self._send_prompt(
            prompt,
            expected_turn_key,
            acceptance_timeout=acceptance_timeout,
            require_message_id=False,
            allow_blocked=True,
            allow_active=True,
        )

    def temporary_chat_state(self) -> Mapping[str, object]:
        value = self.protocol.evaluate(
            self.session_id,
            TEMPORARY_CHAT_STATE_JS,
        )
        if not isinstance(value, Mapping):
            raise RuntimeError("temporary chat state returned a non-object payload")
        return value

    def enable_temporary_chat(
        self,
        *,
        timeout: float = 10.0,
        poll_interval: float = 0.1,
    ) -> Mapping[str, object]:
        deadline = time.monotonic() + timeout
        clicked = False
        last_state: Mapping[str, object] | None = None
        while time.monotonic() < deadline:
            state = self.temporary_chat_state()
            last_state = state
            if (
                state.get("composerReady") is True
                and state.get("isTemporaryChat") is True
            ):
                return state
            if (
                not clicked
                and state.get("composerReady") is True
                and state.get("buttonFound") is True
                and state.get("buttonVisible") is True
                and state.get("buttonDisabled") is not True
            ):
                result = self.protocol.evaluate(
                    self.session_id,
                    ENABLE_TEMPORARY_CHAT_JS,
                )
                if not isinstance(result, Mapping):
                    raise RuntimeError("temporary chat toggle returned a non-object payload")
                if result.get("clicked") is not True and result.get("alreadyTemporary") is not True:
                    reason = str(result.get("reason") or "temporary-chat-toggle-rejected")
                    raise RuntimeError(reason)
                clicked = True
            time.sleep(poll_interval)

        raise TimeoutError(
            "temporary chat did not become active"
            + (f": {dict(last_state)}" if last_state else "")
        )

    def stage_isolated_prompt(
        self,
        prompt: str,
        *,
        chunk_chars: int = 4000,
    ) -> Mapping[str, object]:
        if not isinstance(prompt, str) or not prompt:
            raise ValueError("isolated prompt must be a non-empty string")
        if chunk_chars <= 0:
            raise ValueError("chunk_chars must be positive")

        state = self.temporary_chat_state()
        if state.get("isTemporaryChat") is not True:
            raise RuntimeError("isolated prompt requires an active Temporary Chat")
        if state.get("composerReady") is not True:
            raise RuntimeError("temporary chat composer is not ready")

        self.protocol.evaluate(
            self.session_id,
            "globalThis.__watchdogPromptChunks = []; true",
        )
        for start in range(0, len(prompt), chunk_chars):
            chunk = prompt[start : start + chunk_chars]
            expression = (
                "globalThis.__watchdogPromptChunks.push("
                + json.dumps(chunk)
                + "); globalThis.__watchdogPromptChunks.length"
            )
            self.protocol.evaluate(self.session_id, expression)

        pasted = self.protocol.evaluate(
            self.session_id,
            PASTE_STAGED_COMPOSER_JS,
        )
        if not isinstance(pasted, Mapping) or pasted.get("pasted") is not True:
            reason = (
                str(pasted.get("reason"))
                if isinstance(pasted, Mapping)
                else "invalid-paste-result"
            )
            raise RuntimeError(f"temporary prompt paste failed: {reason}")

        fingerprint = self.protocol.evaluate(
            self.session_id,
            COMPOSER_FINGERPRINT_JS,
            await_promise=True,
        )
        if not isinstance(fingerprint, Mapping) or fingerprint.get("ok") is not True:
            raise RuntimeError("temporary prompt fingerprint failed")

        expected_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        if int(fingerprint.get("chars", -1)) != len(prompt):
            raise RuntimeError(
                "temporary prompt length mismatch: "
                f"{fingerprint.get('chars')} != {len(prompt)}"
            )
        if str(fingerprint.get("sha256") or "") != expected_sha:
            raise RuntimeError("temporary prompt SHA-256 mismatch")
        if fingerprint.get("sendReady") is not True:
            raise RuntimeError("temporary prompt staged but send is not ready")
        return fingerprint

    def send_isolated_prompt(
        self,
        prompt: str,
        *,
        acceptance_timeout: float = 45.0,
        chunk_chars: int = 4000,
    ) -> PromptDelivery:
        before = self.snapshot()
        if before.user_count or before.assistant_count or before.composer_has_draft:
            return PromptDelivery(accepted=False, stale=True)

        self.stage_isolated_prompt(prompt, chunk_chars=chunk_chars)
        result = self.protocol.evaluate(
            self.session_id,
            SUBMIT_STAGED_COMPOSER_JS,
        )
        if not isinstance(result, Mapping) or result.get("submitted") is not True:
            return PromptDelivery(accepted=False)

        deadline = time.monotonic() + acceptance_timeout
        while time.monotonic() < deadline:
            current = self.snapshot()
            if (
                current.user_turn_id
                and current.user_turn_id != before.user_turn_id
                and not current.composer_has_draft
            ):
                return PromptDelivery(
                    accepted=True,
                    message_id=current.user_turn_id,
                )
            time.sleep(0.1)
        return PromptDelivery(accepted=False, uncertain=True)

    def close_target(self) -> None:
        with suppress(Exception):
            self.protocol.command("Target.closeTarget", {"targetId": self.target_id})
        self.close()

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
        exact_urls = {str(target.get("url", "")) for target in matches}
        if len(exact_urls) == 1:
            # The same conversation can be opened twice in the same browser.
            # That is duplicate transport state, not an identity ambiguity.
            # Prefer the original deterministic target id and preserve the
            # fail-closed behavior for distinct matching URLs. A later duplicate
            # tab can lag behind the actively used conversation DOM.
            return min(matches, key=lambda target: str(target.get("id", "")))
        urls = ", ".join(str(target.get("url", "")) for target in matches)
        raise RelayTargetSelectionError(
            f"multiple matching ChatGPT targets; use a more specific --match-url: {urls}"
        )
    return matches[0]
