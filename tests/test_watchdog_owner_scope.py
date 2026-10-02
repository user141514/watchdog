import unittest

from chat_watchdog.intent_client import SidecarIntentClient

TARGET = "https://chatgpt.com/c/00000000-0000-0000-0000-000000000123"
REGISTRATION = "10000000-0000-0000-0000-000000000001"
STATE = {
    "contractVersion": 1, "conversationId": "conversation-123", "target": TARGET,
    "stateVersion": 9,
    "turn": {"turnId": "turn-9", "userMessageId": "user-9", "assistantMessageId": "assistant-9"},
    "progress": "blocked", "body": "incomplete", "delivery": "delivered", "gate": "none",
    "writer": {"mode": "managed", "epoch": 3},
}


class WatchdogOwnerScopeTests(unittest.TestCase):
    def test_ungranted_watchdog_intent_never_reaches_owner(self):
        client = SidecarIntentClient(request_json=lambda *_: self.fail("ungranted request"))
        with self.assertRaisesRegex(ValueError, "registration"):
            client.submit_v1(STATE, "continue task")

    def test_prepared_intent_identity_is_known_without_post_and_matches_dispatch(self):
        calls = []
        client = SidecarIntentClient(request_json=lambda endpoint, payload: (
            calls.append((endpoint, payload)) or {"accepted": True}))
        prepared = client.prepare_v1(STATE, "continue task", registration_id=REGISTRATION)
        self.assertEqual(calls, [])
        client.submit_v1(STATE, "continue task", registration_id=REGISTRATION)
        self.assertEqual(prepared, calls[0][1]["intent"])

    def test_registered_intent_wraps_strict_v1_payload_in_owner_scope(self):
        calls = []
        client = SidecarIntentClient(request_json=lambda endpoint, payload: (
            calls.append((endpoint, payload)) or {"accepted": True}))
        client.submit_v1(STATE, "continue task", registration_id=REGISTRATION)
        endpoint, packet = calls[0]
        self.assertTrue(endpoint.endswith("/internal/watchdog-intents"))
        self.assertEqual(set(packet), {"registrationId", "intent"})
        self.assertEqual(packet["registrationId"], REGISTRATION)
        self.assertEqual(packet["intent"]["expectedStateVersion"], 9)
        self.assertEqual(packet["intent"]["expectedWriterEpoch"], 3)

    def test_bind_and_withdraw_verify_matching_scope_and_quiescent_receipt(self):
        calls = []
        def request(endpoint, payload):
            calls.append((endpoint, payload))
            return {"accepted": True, "quiescent": True,
                    "registrationId": REGISTRATION, "target": TARGET}
        client = SidecarIntentClient(request_json=request)
        client.bind_watch(REGISTRATION, TARGET)
        client.withdraw_watch(REGISTRATION, TARGET)
        self.assertTrue(calls[0][0].endswith("/internal/watchdog-bind"))
        self.assertTrue(calls[1][0].endswith("/internal/watchdog-withdraw"))
        self.assertEqual(calls[1][1], {"registrationId": REGISTRATION, "target": TARGET})

    def test_project_route_binding_accepts_canonical_same_conversation_receipt(self):
        project_target = "https://chatgpt.com/g/g-p-project/c/00000000-0000-0000-0000-000000000123"
        client = SidecarIntentClient(request_json=lambda *_: {
            "accepted": True, "registrationId": REGISTRATION, "target": TARGET,
        })
        self.assertTrue(client.bind_watch(REGISTRATION, project_target)["accepted"])

    def test_binding_rejects_wrong_or_invalid_canonical_conversation_receipt(self):
        for returned_target in (
            "https://chatgpt.com/c/00000000-0000-0000-0000-000000000999",
            TARGET + "?another=target",
            "https://remote.example/c/00000000-0000-0000-0000-000000000123",
        ):
            with self.subTest(returned_target=returned_target):
                client = SidecarIntentClient(request_json=lambda *_, returned_target=returned_target: {
                    "accepted": True, "registrationId": REGISTRATION, "target": returned_target,
                })
                with self.assertRaises(RuntimeError):
                    client.bind_watch(REGISTRATION, TARGET)

    def test_unknown_or_wrong_scope_withdraw_ack_does_not_report_quiescence(self):
        for receipt in (
            {"accepted": True, "registrationId": REGISTRATION},
            {"accepted": True, "quiescent": False, "registrationId": REGISTRATION},
            {"accepted": True, "quiescent": True, "registrationId": "wrong"},
        ):
            with self.subTest(receipt=receipt):
                client = SidecarIntentClient(request_json=lambda *_, receipt=receipt: receipt)
                with self.assertRaises(RuntimeError):
                    client.withdraw_watch(REGISTRATION, TARGET)

    def test_refresh_is_scoped_and_bound_to_version_epoch_exact_ids(self):
        calls = []
        client = SidecarIntentClient(request_json=lambda endpoint, payload: (
            calls.append((endpoint, payload)) or {"accepted": True}))
        client.refresh_v1(STATE, registration_id=REGISTRATION)
        endpoint, packet = calls[0]
        self.assertTrue(endpoint.endswith("/internal/conversation-recovery"))
        self.assertEqual(packet["registrationId"], REGISTRATION)
        self.assertEqual(packet["kind"], "refresh")
        self.assertEqual(packet["expectedStateVersion"], 9)
        self.assertEqual(packet["expectedWriterEpoch"], 3)
        self.assertEqual(packet["expected"], {
            "userMessageId": "user-9", "assistantMessageId": "assistant-9",
        })


if __name__ == "__main__":
    unittest.main()
