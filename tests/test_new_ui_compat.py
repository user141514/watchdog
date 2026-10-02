
from __future__ import annotations

import json
import subprocess
import unittest

from chat_watchdog.dom_snapshot import DOM_SNAPSHOT_JS
from chat_watchdog.model import Phase
from chat_watchdog.relay_page import RelayChatGPTPage, _build_submit_expression


URL = "https://chatgpt.com/g/g-p-test/c/new-ui-fixture"

SNAPSHOT_FIXTURE = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const {source, completed, pendingUser} = JSON.parse(fs.readFileSync(0, 'utf8'));

class Element {
  constructor(text='') {
    this.innerText = text;
    this.textContent = text;
    this.className = '';
  }
  getBoundingClientRect() { return {width: 20, height: 20}; }
  getAttribute(name) {
    if (name === 'aria-disabled') return null;
    return null;
  }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  closest() { return this; }
  cloneNode() {
    const text = this.textContent;
    return {textContent: text, querySelectorAll() { return []; }};
  }
}

const user = new Element('new user prompt');
user.className = 'bg-user-message';
user['__reactFiber$fixture'] = {
  memoizedProps: {turnId: pendingUser ? 'turn-2' : 'turn-1'},
  return: {
    memoizedProps: {
      messageId: 'user-message-1',
      turnId: pendingUser ? 'turn-2' : 'turn-1',
      message: 'new user prompt'
    },
    return: null
  }
};

const assistant = new Element('assistant response\\n[SUPERVISOR_STATE: NEED_INPUT]');
assistant.className = 'MarkdownRoot-fixture';
assistant['__reactFiber$fixture'] = {
  memoizedProps: {conversationId: 'chatgpt:new-ui-fixture'},
  return: {
    memoizedProps: {
      turnId: 'turn-1',
      completed: completed === true,
      activeReasoning: completed !== true,
      isMostRecentTurn: true
    },
    return: null
  }
};

const composer = new Element('');
composer.getAttribute = name => name === 'aria-disabled' ? null : null;
const stop = new Element('');
stop.tagName = 'BUTTON';
stop.getAttribute = name => name === 'aria-label' ? '停止' : null;

const context = {
  window: {
    getComputedStyle: () => ({display: 'block', visibility: 'visible'})
  },
  document: {
    addEventListener() {},
    querySelector(selector) {
      if (selector === '[contenteditable="true"][role="textbox"]') return composer;
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
      if (selector === 'button') return completed === true ? [] : [stop];
      return [];
    }
  },
  Date,
  Set,
  String,
  Number
};

console.log(JSON.stringify(vm.runInNewContext('(' + source + ')()', context, {timeout: 1000})));
"""

SUBMIT_FIXTURE = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const {expression, completed, draft} = JSON.parse(fs.readFileSync(0, 'utf8'));

let submitted = 0;
let clicked = 0;
let unrelatedClicked = 0;

class Element {
  getBoundingClientRect() { return {width: 20, height: 20}; }
  getAttribute() { return null; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  compareDocumentPosition() { return 0; }
  closest() { return null; }
}
class Textarea extends Element {}
class Input extends Element {}
class ContentEditable extends Element {
  constructor() {
    super();
    this.innerText = draft || '';
    this.textContent = draft || '';
  }
  focus() {}
  closest(selector) { return selector === 'form' ? form : null; }
}

const user = new Element();
user.className = 'bg-user-message';
user['__reactFiber$fixture'] = {
  memoizedProps: {turnId: 'turn-1'},
  return: {memoizedProps: {messageId: 'user-1', turnId: 'turn-1'}, return: null}
};

const assistant = new Element();
assistant.className = 'MarkdownRoot-fixture';
assistant['__reactFiber$fixture'] = {
  memoizedProps: {conversationId: 'chatgpt:new-ui-fixture'},
  return: {
    memoizedProps: {turnId: 'turn-1', completed: completed === true, isMostRecentTurn: true},
    return: null
  }
};

const button = new Element();
button.disabled = false;
button.getAttribute = name => {
  if (name === 'aria-disabled') return 'false';
  if (name === 'aria-label') return '发送';
  if (name === 'type') return 'submit';
  return null;
};
button.click = () => { clicked += 1; };
const unrelatedButton = new Element();
unrelatedButton.getAttribute = name => name === 'aria-label' ? '发送' : null;
unrelatedButton.click = () => { unrelatedClicked += 1; };

const form = new Element();
form.querySelector = selector => selector === 'button[type="submit"]' ? button : null;
form.requestSubmit = candidate => {
  if (candidate !== button) throw new Error('wrong submit button');
  submitted += 1;
};

const editor = new ContentEditable();

const context = {
  document: {
    querySelector(selector) {
      if (selector === '#prompt-textarea') return null;
      if (selector === '[contenteditable="true"][data-lexical-editor="true"]') return null;
      if (selector === '[contenteditable="true"][role="textbox"]') return editor;
      if (selector === '[data-testid="stop-button"]') return null;
      if (selector === '[data-testid="send-button"]') return null;
      return null;
    },
    querySelectorAll(selector) {
      if (selector === '[data-message-author-role="assistant"]') return [];
      if (selector === '[data-message-author-role="user"]') return [];
      if (selector === '[class*="MarkdownRoot-"]') return [assistant];
      if (selector === '.bg-user-message') return [user];
      if (selector === 'button') return [unrelatedButton, button];
      return [];
    },
    execCommand(command, _showUi, value) {
      if (command === 'selectAll') return true;
      if (command === 'insertText') {
        editor.innerText = value;
        editor.textContent = value;
        return true;
      }
      return false;
    }
  },
  window: {getComputedStyle: () => ({display: 'block', visibility: 'visible'})},
  HTMLTextAreaElement: Textarea,
  HTMLInputElement: Input,
  InputEvent: class {},
  Object,
  String,
  setTimeout: fn => fn()
};

(async () => {
  const result = await vm.runInNewContext(expression, context, {timeout: 1000});
  console.log(JSON.stringify({
    result,
    submitted,
    clicked,
    unrelatedClicked,
    draft: editor.innerText
  }));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


class NewUiCompatibilityTests(unittest.TestCase):
    @staticmethod
    def snapshot(payload):
        class Protocol:
            def evaluate(self, _session, expression):
                return URL if expression == "location.href" else payload

        page = RelayChatGPTPage("page", URL, "session", object(), Protocol(), URL)
        return page.snapshot()

    def snapshot_payload(self, *, completed: bool, pending_user: bool = False) -> dict:
        run = subprocess.run(
            ["node", "-e", SNAPSHOT_FIXTURE],
            input=json.dumps(
                {
                    "source": DOM_SNAPSHOT_JS,
                    "completed": completed,
                    "pendingUser": pending_user,
                }
            ),
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=10,
            check=True,
        )
        return json.loads(run.stdout)

    def submit_result(self, *, completed: bool = True, draft: str = "") -> dict:
        run = subprocess.run(
            ["node", "-e", SUBMIT_FIXTURE],
            input=json.dumps(
                {
                    "expression": _build_submit_expression("fixture-only"),
                    "completed": completed,
                    "draft": draft,
                }
            ),
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=10,
            check=True,
        )
        return json.loads(run.stdout)

    def test_new_ui_semantic_ids_and_completed_state(self):
        payload = self.snapshot_payload(completed=True)
        snap = self.snapshot(payload)
        self.assertEqual(snap.assistant_turn_id, "turn-1")
        self.assertEqual(snap.user_turn_id, "user-message-1")
        self.assertFalse(snap.user_turn_pending)
        self.assertEqual(snap.phase, Phase.FINISHED)
        self.assertTrue(snap.assistant_text.endswith("[SUPERVISOR_STATE: NEED_INPUT]"))

    def test_new_ui_active_turn_uses_semantic_completed_flag(self):
        payload = self.snapshot_payload(completed=False)
        self.assertEqual(self.snapshot(payload).phase, Phase.THINKING)

    def test_new_ui_newer_user_turn_is_pending(self):
        payload = self.snapshot_payload(completed=True, pending_user=True)
        snap = self.snapshot(payload)
        self.assertTrue(snap.user_turn_pending)
        self.assertEqual(snap.phase, Phase.BLOCKED)

    def test_new_ui_submit_clicks_only_composer_form_send_button(self):
        actual = self.submit_result()
        self.assertTrue(actual["result"]["submitted"])
        self.assertEqual(actual["submitted"], 0)
        self.assertEqual(actual["clicked"], 1)
        self.assertEqual(actual["unrelatedClicked"], 0)
        self.assertEqual(actual["draft"], "fixture-only")

    def test_new_ui_active_generation_blocks_submit(self):
        actual = self.submit_result(completed=False)
        self.assertFalse(actual["result"]["submitted"])
        self.assertEqual(actual["result"]["reason"], "generation-active")
        self.assertEqual(actual["submitted"], 0)


if __name__ == "__main__":
    unittest.main()
