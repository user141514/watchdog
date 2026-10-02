from __future__ import annotations

import json
import subprocess
import unittest

from chat_watchdog.dom_snapshot import DOM_SNAPSHOT_JS
from chat_watchdog.model import Phase
from chat_watchdog.relay_page import RelayChatGPTPage

URL = "https://chatgpt.com/c/modern-fixture"

NODE_FIXTURE = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const {source, config} = JSON.parse(fs.readFileSync(0, 'utf8'));

function visibleElement() {
  return {
    getBoundingClientRect() { return {width: 20, height: 20}; },
    getAttribute() { return null; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    closest() { return null; },
  };
}

function fiberChain(propsList) {
  let next = null;
  for (let i = propsList.length - 1; i >= 0; i -= 1) {
    next = {memoizedProps: propsList[i], return: next};
  }
  return next;
}

const turnId = 'fallback-turn-7';
const assistant = {
  ...visibleElement(),
  innerText: config.text || 'modern assistant answer',
  textContent: config.text || 'modern assistant answer',
  className: 'MarkdownRoot-fixture',
  cloneNode() {
    return {
      textContent: config.text || 'modern assistant answer',
      querySelectorAll() { return []; },
    };
  },
};
assistant.__reactFiber$fixture = fiberChain([
  {turnId, isStreaming: true},
  {turnId, isMostRecentTurn: true, isStreaming: config.streaming},
  {completed: config.completed},
]);

const user = {
  ...visibleElement(),
  innerText: 'modern user prompt',
  textContent: 'modern user prompt',
  className: 'bg-user-message',
};
user.__reactFiber$fixture = fiberChain([
  {turnId},
  {turnId, messageId: 'modern-user-message-id'},
]);

const composer = {
  ...visibleElement(),
  innerText: '',
  textContent: '',
  className: 'ProseMirror',
  getAttribute(name) {
    if (name === 'role') return 'textbox';
    if (name === 'contenteditable') return 'true';
    if (name === 'aria-disabled') return null;
    return null;
  },
};

const stop = {
  ...visibleElement(),
  tagName: 'BUTTON',
  textContent: '',
  getAttribute(name) {
    if (name === 'aria-label') return 'Stop';
    return null;
  },
};

const context = {
  window: {
    getComputedStyle: () => ({display: 'block', visibility: 'visible'}),
  },
  document: {
    addEventListener() {},
    querySelector(selector) {
      if (selector === '#prompt-textarea') return null;
      if (selector === '[contenteditable="true"][data-lexical-editor="true"]') return null;
      if (selector === '[contenteditable="true"][role="textbox"]') return composer;
      if (selector === 'div[contenteditable="true"]') return composer;
      if (selector === '[data-testid="stop-button"]') return null;
      return null;
    },
    querySelectorAll(selector) {
      if (selector === '[data-message-author-role="assistant"]') return [];
      if (selector === '[data-message-author-role="user"]') return [];
      if (selector === '[data-testid^="conversation-turn-"][data-turn="assistant"], article[data-turn="assistant"]') return [];
      if (selector === '[class*="MarkdownRoot-"]') return [assistant];
      if (selector === '.bg-user-message') return [user];
      if (selector === '[data-testid="tool-approval-card"]') return [];
      if (selector === 'button') return config.streaming ? [stop] : [];
      return [];
    },
  },
  Date,
  Set,
  String,
  Number,
  Object,
};

console.log(JSON.stringify(vm.runInNewContext(`(${source})()`, context, {timeout: 1000})));
"""


class ModernUiSnapshotTests(unittest.TestCase):
    def payload(self, *, streaming: bool, completed: bool, text: str = "modern assistant answer"):
        run = subprocess.run(
            ["node", "-e", NODE_FIXTURE],
            input=json.dumps(
                {
                    "source": DOM_SNAPSHOT_JS,
                    "config": {"streaming": streaming, "completed": completed, "text": text},
                }
            ),
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=10,
            check=True,
        )
        return json.loads(run.stdout)

    @staticmethod
    def snapshot(payload):
        class Protocol:
            def evaluate(self, _session, expression):
                return URL if expression == "location.href" else payload

        page = RelayChatGPTPage("page", URL, "session", object(), Protocol(), URL)
        return page.snapshot()

    def test_modern_completed_turn_uses_deepest_react_state_and_is_finished(self):
        payload = self.payload(streaming=False, completed=True, text="WATCHDOG_UI_COMPAT_OK")
        snap = self.snapshot(payload)
        self.assertEqual(snap.phase, Phase.FINISHED)
        self.assertEqual(snap.assistant_turn_id, "fallback-turn-7")
        self.assertEqual(snap.user_turn_id, "modern-user-message-id")
        self.assertEqual(snap.assistant_text, "WATCHDOG_UI_COMPAT_OK")
        self.assertFalse(snap.stop_visible)
        self.assertFalse(snap.user_turn_pending)

    def test_modern_streaming_turn_remains_active(self):
        payload = self.payload(streaming=True, completed=False)
        snap = self.snapshot(payload)
        self.assertEqual(snap.phase, Phase.RESPONDING)
        self.assertTrue(snap.stop_visible)
        self.assertEqual(snap.assistant_turn_id, "fallback-turn-7")


if __name__ == "__main__":
    unittest.main()
