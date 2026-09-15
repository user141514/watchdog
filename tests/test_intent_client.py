import unittest
from chat_watchdog.model import PageSnapshot, Phase
from chat_watchdog.cli import build_parser
try:
    from chat_watchdog.intent_client import SidecarIntentClient
except ImportError:
    SidecarIntentClient = None

class IntentClientTests(unittest.TestCase):
    def test_client_sends_both_message_ids_and_no_admission_request(self):
        self.assertIsNotNone(SidecarIntentClient)
        calls = []
        def request(endpoint, payload):
            calls.append((endpoint, payload))
            return {'accepted': True, 'turnId': 'accepted-once'}
        endpoint = 'http://127.0.0.1:7337/internal/conversation-intents'
        client = SidecarIntentClient(endpoint, request_json=request)
        snapshot = PageSnapshot(Phase.FINISHED, 'a1', 'sig', 'body', 1, 1, user_turn_id='u1')
        result = client.submit('https://chatgpt.com/c/00000000-0000-0000-0000-000000000001', snapshot, 'continue')
        self.assertTrue(result['accepted'])
        self.assertEqual(calls[0][0], endpoint)
        self.assertEqual(calls[0][1]['expected'], {'userMessageId': 'u1', 'assistantMessageId': 'a1'})
        self.assertEqual(calls[0][1]['kind'], 'continue')
    def test_nonlocal_owner_and_missing_identity_are_rejected(self):
        self.assertIsNotNone(SidecarIntentClient)
        with self.assertRaises(ValueError): SidecarIntentClient('https://remote.example/internal/conversation-intents')
        client = SidecarIntentClient('http://127.0.0.1:7337/internal/conversation-intents', request_json=lambda *args: self.fail('must not send'))
        with self.assertRaises(ValueError): client.submit('https://chatgpt.com/c/test', PageSnapshot(Phase.FINISHED, 'a', 'sig', 'body', 1, 1), 'continue')
    def test_cli_requires_explicit_legacy_choice(self):
        parser = build_parser()
        args = parser.parse_args([])
        self.assertFalse(getattr(args, 'legacy_direct_send', True))
        self.assertTrue(parser.parse_args(['--legacy-direct-send']).legacy_direct_send)
