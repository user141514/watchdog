"""Regression for a hidden live tab: partial text, no stop button, no final controls.

The observed partial response was '当前目标'; reloading the SAME conversation
revealed a complete valid nonce receipt. Missing stop is not positive finality.
"""
import json
import subprocess
import unittest

from chat_watchdog.dom_snapshot import DOM_SNAPSHOT_JS
from chat_watchdog.model import Phase
from chat_watchdog.relay_page import RelayChatGPTPage

URL = 'https://chatgpt.com/g/g-p-test/c/controlled'
NODE_FIXTURE = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const {source, config} = JSON.parse(fs.readFileSync(0, 'utf8'));
const element = () => ({
  getBoundingClientRect: () => ({width: 20, height: 20}),
  getAttribute: () => null
});
const finalButton = element();
const content = {...element(), innerText: config.final ? 'complete response' : 'partial',
  textContent: config.final ? 'complete response' : 'partial', innerHTML: '<p>text</p>', childElementCount: 1};
const assistantTurn = {
  getAttribute(name) {
    if (name === 'data-testid') return 'conversation-turn-4';
    if (name === 'data-turn-id') return config.final && !config.provisional ? 'persisted-turn-uuid' : 'request-WEB:pending';
    return null;
  },
  querySelector(selector) {
    if (selector.includes('turn-action-button')) return config.final ? finalButton : null;
    if (selector === '.markdown') return content;
    return null;
  },
  querySelectorAll(selector) {
    return selector.includes('turn-action-button') && config.final ? [finalButton] : [];
  }
};
const userTurn = {getAttribute: name => name === 'data-testid' ? 'conversation-turn-3' : null};
const assistant = {getAttribute: name => name === 'data-message-id' ? 'assistant-message-uuid' : null,
  closest: () => assistantTurn};
const user = {getAttribute: name => name === 'data-message-id' ? 'user-message-uuid' : null,
  closest: () => userTurn, innerText: 'fixture prompt', textContent: 'fixture prompt'};
const composer = {...element(), innerText: '', textContent: ''};
const context = {
  window: {getComputedStyle: () => ({display: 'block', visibility: 'visible'})},
  document: {
    querySelector(selector) {
      if (selector === '#prompt-textarea') return composer;
      if (selector === '[data-testid="stop-button"]') return config.active ? element() : null;
      return null;
    },
    querySelectorAll(selector) {
      if (selector === '[data-message-author-role="assistant"]') return [assistant];
      if (selector === '[data-message-author-role="user"]') return [user];
      return [];
    }
  }
};
console.log(JSON.stringify(vm.runInNewContext(`(${source})()`, context, {timeout: 1000})));
"""


class SnapshotFinalityTests(unittest.TestCase):
    def payload(self, **config):
        run = subprocess.run(['node', '-e', NODE_FIXTURE],
            input=json.dumps({'source': DOM_SNAPSHOT_JS, 'config': config}),
            text=True, encoding='utf-8', capture_output=True, timeout=10, check=True)
        return json.loads(run.stdout)

    @staticmethod
    def snapshot(payload):
        class Protocol:
            def evaluate(self, _session, expression):
                return URL if expression == 'location.href' else payload
        page = RelayChatGPTPage('page', URL, 'session', object(), Protocol(), URL)
        return page.snapshot()

    def test_partial_response_without_stop_is_not_final(self):
        payload = self.payload(final=False)
        self.assertEqual(self.snapshot(payload).phase, Phase.BLOCKED)
        self.assertIs(payload.get('assistantFinalized'), False)

    def test_final_controls_allow_completed_reply(self):
        payload = self.payload(final=True)
        self.assertIs(payload.get('assistantFinalized'), True)
        self.assertEqual(self.snapshot(payload).phase, Phase.FINISHED)

    def test_final_controls_outweigh_provisional_container_id(self):
        # Live ChatGPT retains request-* container IDs even after final controls appear.
        payload = self.payload(final=True, provisional=True)
        self.assertEqual(self.snapshot(payload).phase, Phase.FINISHED)
        self.assertEqual(payload['assistantTurnId'], 'assistant-message-uuid')

    def test_missing_finality_signal_fails_closed(self):
        payload = self.payload(final=True)
        payload.pop('assistantFinalized', None)
        self.assertEqual(self.snapshot(payload).phase, Phase.BLOCKED)

    def test_active_generation_remains_active(self):
        self.assertEqual(self.snapshot(self.payload(final=False, active=True)).phase, Phase.RESPONDING)

    def test_persistent_message_ids_replace_dom_position_numbers(self):
        payload = self.payload(final=True)
        self.assertEqual(payload['assistantTurnId'], 'assistant-message-uuid')
        self.assertEqual(payload['userTurnId'], 'user-message-uuid')


if __name__ == '__main__':
    unittest.main()
