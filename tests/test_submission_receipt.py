from __future__ import annotations

import json
import subprocess
import unittest

from chat_watchdog.dom_snapshot import DOM_SNAPSHOT_JS


NODE_FIXTURE = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const {source, fire, trusted, remount} = JSON.parse(fs.readFileSync(0, 'utf8'));

const listeners = {};
const element = () => ({
  getBoundingClientRect: () => ({width: 20, height: 20}),
  getAttribute: () => null,
});

const content = {
  ...element(),
  innerText: 'assistant body',
  textContent: 'assistant body',
  innerHTML: '<p>assistant body</p>',
  childElementCount: 1,
};
const assistantTurn = {
  innerHTML: '<div>assistant body</div>',
  childElementCount: 1,
  getAttribute(name) {
    if (name === 'data-turn-id') return 'assistant-stable-turn';
    return null;
  },
  querySelector(selector) {
    if (selector === '.markdown') return content;
    return null;
  },
  querySelectorAll() { return []; },
  cloneNode() {
    return {
      textContent: 'assistant body',
      querySelectorAll() { return []; },
    };
  },
};
const assistant = {
  getAttribute(name) {
    if (name === 'data-message-id') return 'assistant-message-1';
    return null;
  },
  closest() { return assistantTurn; },
  compareDocumentPosition(other) { return other?._causal === true ? 4 : 0; },
};

function makeUser(id, text, causal = false) {
  const turn = {
    textContent: text,
    getAttribute(name) {
      if (name === 'data-turn-id') return id + '-turn';
      return null;
    },
  };
  return {
    _id: id,
    _causal: causal,
    innerText: text,
    textContent: text,
    getAttribute(name) {
      if (name === 'data-message-id') return id;
      return null;
    },
    closest() { return turn; },
  };
}

let users = [makeUser('user-old', 'old prompt')];
let composerText = 'new prompt';
const composer = {
  ...element(),
  get value() { return composerText; },
  set value(value) { composerText = value; },
  innerText: '',
  textContent: '',
  getAttribute(name) {
    if (name === 'aria-disabled') return null;
    return null;
  },
};
const sendButton = {
  tagName: 'BUTTON',
  textContent: 'Send',
  getAttribute(name) {
    if (name === 'data-testid') return 'send-button';
    if (name === 'aria-label') return 'Send';
    return null;
  },
  closest(selector) {
    return selector === 'button' ? this : null;
  },
};

const context = vm.createContext({
  window: {
    getComputedStyle: () => ({display: 'block', visibility: 'visible'}),
  },
  document: {
    addEventListener(type, callback) {
      (listeners[type] ||= []).push(callback);
    },
    querySelector(selector) {
      if (selector === '#prompt-textarea') return composer;
      if (selector === '[data-testid="stop-button"]') return null;
      return null;
    },
    querySelectorAll(selector) {
      if (selector === '[data-message-author-role="assistant"]') return [assistant];
      if (selector === '[data-message-author-role="user"]') return users;
      if (selector === '[data-testid="tool-approval-card"]') return [];
      if (selector === 'button') return [];
      return [];
    },
  },
  Date,
  Set,
  String,
  Number,
});

function snapshot() {
  return vm.runInContext(`(${source})()`, context, {timeout: 1000});
}

const before = snapshot();
if (fire) {
  for (const callback of listeners.click || []) {
    callback({target: sendButton, isTrusted: trusted === true});
  }
}
users = remount
  ? [makeUser('user-old', 'old prompt'), makeUser('user-remount', 'new prompt', false)]
  : [makeUser('user-old', 'old prompt'), makeUser('user-new', 'new prompt', true)];
const after = snapshot();
console.log(JSON.stringify({before, after}));
"""


class SubmissionReceiptTests(unittest.TestCase):
    def run_fixture(self, *, fire: bool, trusted: bool, remount: bool = False) -> dict:
        run = subprocess.run(
            ["node", "-e", NODE_FIXTURE],
            input=json.dumps(
                {
                    "source": DOM_SNAPSHOT_JS,
                    "fire": fire,
                    "trusted": trusted,
                    "remount": remount,
                }
            ),
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=10,
            check=True,
        )
        return json.loads(run.stdout)

    def test_programmatic_submit_binds_exact_untrusted_receipt(self) -> None:
        result = self.run_fixture(fire=True, trusted=False)
        self.assertEqual(result["before"]["submissionSeq"], 0)
        self.assertEqual(result["after"]["submissionSeq"], 1)
        self.assertEqual(result["after"]["submissionReceiptSeq"], 1)
        self.assertEqual(result["after"]["submissionReceiptId"], "user-new")
        self.assertEqual(result["after"]["submissionReceiptText"], "new prompt")
        self.assertEqual(result["after"]["trustedSubmissionReceiptSeq"], 0)
        self.assertEqual(result["after"]["trustedSubmissionReceiptId"], "")

    def test_trusted_human_submit_binds_trusted_receipt(self) -> None:
        result = self.run_fixture(fire=True, trusted=True)
        self.assertEqual(result["after"]["submissionReceiptSeq"], 1)
        self.assertEqual(result["after"]["submissionReceiptId"], "user-new")
        self.assertEqual(result["after"]["trustedSubmissionReceiptSeq"], 1)
        self.assertEqual(result["after"]["trustedSubmissionReceiptId"], "user-new")

    def test_dom_remount_without_submit_event_cannot_create_receipt(self) -> None:
        result = self.run_fixture(fire=False, trusted=False)
        self.assertEqual(result["after"]["submissionSeq"], 0)
        self.assertEqual(result["after"]["submissionReceiptSeq"], 0)
        self.assertEqual(result["after"]["submissionReceiptId"], "")
        self.assertEqual(result["after"]["trustedSubmissionReceiptSeq"], 0)

    def test_old_same_text_remount_after_submit_cannot_bind_receipt(self) -> None:
        result = self.run_fixture(fire=True, trusted=False, remount=True)
        self.assertEqual(result["after"]["submissionSeq"], 1)
        self.assertEqual(result["after"]["submissionReceiptSeq"], 0)
        self.assertEqual(result["after"]["submissionReceiptId"], "")


if __name__ == "__main__":
    unittest.main()
