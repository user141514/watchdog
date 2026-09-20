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
const contentValue = config.contentText || (config.final ? 'complete response' : 'partial');
const content = {...element(), innerText: contentValue,
  textContent: contentValue, innerHTML: '<p>text</p>', childElementCount: 1};
const assistantTurn = {
  getAttribute(name) {
    if (config.noStableIds) return null;
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
  },
  cloneNode() {
    return {
      textContent: config.fullText || contentValue,
      querySelectorAll() { return []; }
    };
  }
};
const userTurn = {getAttribute: name => !config.noStableIds && name === 'data-testid' ? 'conversation-turn-3' : null};
const pendingUser = {getAttribute: name => !config.noStableIds && !config.noMessageIds && name === 'data-message-id' ? 'pending-user-message-uuid' : null,
  closest: () => userTurn, innerText: 'new coordinator prompt', textContent: 'new coordinator prompt'};
const assistant = {getAttribute: name => !config.noStableIds && !config.noMessageIds && name === 'data-message-id' ? 'assistant-message-uuid' : null,
  closest: () => assistantTurn,
  compareDocumentPosition(other) { return config.pendingUser && other === pendingUser ? 4 : 0; }};
const user = {getAttribute: name => !config.noStableIds && !config.noMessageIds && name === 'data-message-id' ? 'user-message-uuid' : null,
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
      if (selector === '[data-message-author-role="user"]') return config.pendingUser ? [user, pendingUser] : [user];
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

    def test_newer_unanswered_user_turn_blocks_previous_final_assistant(self):
        payload = self.payload(final=True, pendingUser=True)
        self.assertIs(payload.get('assistantFinalized'), True)
        self.assertIs(payload.get('userTurnPending'), True)
        self.assertEqual(self.snapshot(payload).phase, Phase.BLOCKED)

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

    def test_liveness_signature_tracks_later_content_blocks(self):
        first = self.payload(
            final=False,
            active=True,
            contentText='first rendered block',
            fullText='first rendered block\nsecond block v1',
        )
        second = self.payload(
            final=False,
            active=True,
            contentText='first rendered block',
            fullText='first rendered block\nsecond block v2 expanded',
        )
        self.assertEqual(first['assistantText'], 'first rendered block')
        self.assertEqual(second['assistantText'], 'first rendered block')
        self.assertNotEqual(first['assistantTextSignature'], second['assistantTextSignature'])

    def test_terminal_protocol_marker_can_come_from_later_content_block(self):
        full = 'first rendered block\nsecond rendered block\n[SUPERVISOR_STATE: NEED_INPUT]'
        payload = self.payload(
            final=True,
            contentText='first rendered block',
            fullText=full,
        )
        self.assertEqual(payload['assistantText'], full)
        self.assertTrue(payload['assistantText'].endswith('[SUPERVISOR_STATE: NEED_INPUT]'))

    def test_persistent_message_ids_replace_dom_position_numbers(self):
        payload = self.payload(final=True)
        self.assertEqual(payload['assistantTurnId'], 'assistant-message-uuid')
        self.assertEqual(payload['userTurnId'], 'user-message-uuid')

    def test_missing_stable_message_identity_never_falls_back_to_dom_counts(self):
        payload = self.payload(final=True, noStableIds=True)
        self.assertEqual(payload['assistantTurnId'], '')
        self.assertEqual(payload['userTurnId'], '')
        self.assertEqual(self.snapshot(payload).phase, Phase.BLOCKED)

    def test_stable_data_turn_id_remains_a_compatible_identity_fallback(self):
        payload = self.payload(final=True, noMessageIds=True)
        self.assertEqual(payload['assistantTurnId'], 'persisted-turn-uuid')
        self.assertEqual(payload['userTurnId'], '')
        self.assertEqual(self.snapshot(payload).phase, Phase.FINISHED)

    def test_positional_or_provisional_container_ids_are_not_semantic_identity(self):
        payload = self.payload(final=True, noMessageIds=True, provisional=True)
        self.assertEqual(payload['assistantTurnId'], '')
        self.assertEqual(payload['userTurnId'], '')
        self.assertEqual(self.snapshot(payload).phase, Phase.BLOCKED)

    def test_turn_key_is_stable_when_virtualized_dom_counts_change(self):
        before = self.snapshot(self.payload(final=True))
        after_payload = self.payload(final=True)
        after_payload['assistantCount'] = 1
        after_payload['userCount'] = 2
        after = self.snapshot(after_payload)
        self.assertEqual(before.turn_key, after.turn_key)


if __name__ == '__main__':
    unittest.main()
