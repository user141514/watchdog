"""Exercise the actual browser submit expression in a minimal DOM fixture."""
import json
import subprocess
import unittest

from chat_watchdog.relay_page import _build_submit_expression

NODE_FIXTURE = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const {expression, config} = JSON.parse(fs.readFileSync(0, 'utf8'));
let clicks = 0;
class Element {
  getBoundingClientRect() { return {width: 10, height: 10}; }
  getAttribute(name) { return null; }
  closest() { return this; }
  querySelector() { return null; }
  dispatchEvent() {}
  focus() {}
}
class Textarea extends Element { constructor() { super(); this.value = config.draft || ''; } }
class Input extends Element {}
const editor = new Textarea();
const button = new Element();
const assistant = new Element();
const pendingUser = new Element();
assistant.compareDocumentPosition = other => config.pendingUser && other === pendingUser ? 4 : 0;
button.disabled = Boolean(config.disabled);
button.getAttribute = name => name === 'aria-disabled' ? String(Boolean(config.ariaDisabled)) : null;
button.click = () => { clicks += 1; };
const context = {
  document: {
    querySelector(selector) {
      if (selector === '#prompt-textarea') return editor;
      if (selector === '[data-testid="send-button"]') return button;
      if (selector === '[data-testid="stop-button"]') return config.active ? new Element() : null;
      return null;
    },
    querySelectorAll(selector) {
      if (selector === '[data-message-author-role="assistant"]') return [assistant];
      if (selector === '[data-message-author-role="user"]') return config.pendingUser ? [pendingUser] : [];
      return [];
    }
  },
  window: {getComputedStyle: () => ({display: 'block', visibility: 'visible'})},
  HTMLTextAreaElement: Textarea,
  HTMLInputElement: Input,
  InputEvent: class {},
  setTimeout: fn => fn()
};
(async () => {
  const result = await vm.runInNewContext(expression, context, {timeout: 1000});
  console.log(JSON.stringify({result, clicks, draft: editor.value}));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


class SubmitGuardTests(unittest.TestCase):
    def run_expression(self, **config):
        run = subprocess.run(
            ['node', '-e', NODE_FIXTURE],
            input=json.dumps({'expression': _build_submit_expression('fixture-only'), 'config': config}),
            text=True, encoding='utf-8', capture_output=True, timeout=10, check=True,
        )
        return json.loads(run.stdout)

    def test_aria_disabled_button_is_not_clicked_or_reported_submitted(self):
        actual = self.run_expression(ariaDisabled=True)
        self.assertFalse(actual['result']['submitted'])
        self.assertEqual(actual['clicks'], 0)

    def test_enabled_button_is_clicked_once(self):
        actual = self.run_expression()
        self.assertTrue(actual['result']['submitted'])
        self.assertEqual(actual['clicks'], 1)

    def test_native_disabled_button_is_not_clicked(self):
        actual = self.run_expression(disabled=True)
        self.assertFalse(actual['result']['submitted'])
        self.assertEqual(actual['clicks'], 0)

    def test_existing_draft_is_preserved(self):
        actual = self.run_expression(draft='user draft')
        self.assertEqual(actual['result']['reason'], 'composer-not-empty')
        self.assertEqual(actual['draft'], 'user draft')
        self.assertEqual(actual['clicks'], 0)

    def test_exact_existing_watchdog_draft_is_resumed_and_sent(self):
        actual = self.run_expression(draft='fixture-only')
        self.assertTrue(actual['result']['submitted'])
        self.assertEqual(actual['draft'], 'fixture-only')
        self.assertEqual(actual['clicks'], 1)

    def test_active_generation_is_not_interrupted(self):
        actual = self.run_expression(active=True)
        self.assertEqual(actual['result']['reason'], 'generation-active')
        self.assertEqual(actual['draft'], '')
        self.assertEqual(actual['clicks'], 0)

    def test_unanswered_coordinator_user_turn_blocks_watchdog_submit(self):
        actual = self.run_expression(pendingUser=True)
        self.assertFalse(actual['result']['submitted'])
        self.assertEqual(actual['result']['reason'], 'user-turn-pending')
        self.assertEqual(actual['draft'], '')
        self.assertEqual(actual['clicks'], 0)


if __name__ == '__main__':
    unittest.main()
