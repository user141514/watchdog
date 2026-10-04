"""Consume the actual extension/native/owner fixture without rewriting its fields."""
from __future__ import annotations

import copy
from datetime import datetime
import json
from pathlib import Path
import unittest

from chat_watchdog.intent_client import SidecarIntentClient
from chat_watchdog.model import Phase
from chat_watchdog.observation_client import SidecarObservationClient
from chat_watchdog.simple_watchdog import SimpleWatcher


FIXTURE = Path(__file__).parent / "fixtures" / "native-observation-owner-v1.json"


class NativeObservationOwnerTests(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.now = datetime.fromisoformat(
            self.payload["observation"]["observedAt"].replace("Z", "+00:00")
        ).timestamp()

    def runtime(self, payload):
        effects = []
        observation = SidecarObservationClient(
            request_json=lambda endpoint, request: copy.deepcopy(payload), clock=lambda: self.now,
        )
        def record_effect(endpoint, request):
            effects.append(copy.deepcopy(request))
            return {"accepted": True, "messageId": "new-review-user"}
        intents = SidecarIntentClient(request_json=record_effect)
        lineage = self.payload["lineage"]
        runtime = SimpleWatcher(
            self.payload["state"]["target"], observation_client=observation, intent_client=intents,
            registration_id=lineage["registrationId"], sleep=lambda seconds: None,
        )
        runtime.restore_state({
            "phase": 1, "cycle": 0, "next_prompt": "", "status": "RUNNING",
            "transition_turn_id": "prior-action-assistant",
            "expected_review_intent_id": lineage["intentId"],
        })
        return runtime, observation, effects

    def test_actual_native_owner_fixture_finishes_reserved_own_review(self):
        runtime, observations, effects = self.runtime(self.payload)
        sample = observations.read(runtime.target_url)
        self.assertEqual(sample.snapshot().phase, Phase.FINISHED)
        self.assertEqual(runtime.step(), "done")
        self.assertEqual(runtime.durable_state["status"], "DONE")
        self.assertEqual(runtime.step(), "done")
        self.assertEqual(effects, [])

    def test_actual_native_done_body_in_action_phase_advances_to_review(self):
        runtime, _, effects = self.runtime(self.payload)
        runtime.restore_state({
            "phase": 0, "cycle": 0, "next_prompt": "", "status": "RUNNING",
            "transition_turn_id": None, "expected_review_intent_id": None,
        })
        checkpoints = []
        runtime.set_persistence_callback(lambda state: checkpoints.append(copy.deepcopy(state)))
        self.assertEqual(runtime.step(), "transition_sent")
        self.assertEqual(runtime.durable_state["phase"], 1)
        self.assertEqual(runtime.durable_state["status"], "RUNNING")
        self.assertEqual(len(effects), 1)
        effect = effects[0]
        self.assertEqual(effect["registrationId"], self.payload["lineage"]["registrationId"])
        self.assertIn("REVIEW 0", effect["intent"]["text"])
        self.assertEqual(effect["intent"]["expectedStateVersion"], self.payload["state"]["stateVersion"])
        self.assertEqual(effect["intent"]["expectedWriterEpoch"], self.payload["state"]["writer"]["epoch"])
        self.assertEqual(checkpoints[0]["phase"], 0)
        self.assertIsNone(checkpoints[0]["expected_review_intent_id"])
        review_reservations = [state for state in checkpoints if state["phase"] == 1]
        self.assertEqual(len(review_reservations), 1)
        self.assertEqual(review_reservations[0]["expected_review_intent_id"], effect["intent"]["intentId"])

    def test_missing_current_owner_lineage_cannot_finish_review(self):
        payload = copy.deepcopy(self.payload)
        payload["lineage"] = {"registrationId": None, "intentId": None}
        runtime, _, effects = self.runtime(payload)
        self.assertEqual(runtime.step(), "need_input")
        self.assertEqual(runtime.diagnostics["reason"], "review_lineage_unverified")
        self.assertEqual(runtime.durable_state["status"], "RUNNING")
        self.assertEqual(effects, [])

    def test_actual_native_shape_with_unsettled_delivery_never_finishes_or_writes(self):
        for delivery in ("unknown", "none", "pending", "uncertain"):
            with self.subTest(delivery=delivery):
                payload = copy.deepcopy(self.payload)
                payload["observation"]["delivery"] = delivery
                runtime, observations, effects = self.runtime(payload)
                sample = observations.read(runtime.target_url)
                self.assertEqual(sample.snapshot().phase, Phase.BLOCKED)
                self.assertNotEqual(runtime.step(), "done")
                self.assertEqual(runtime.durable_state["status"], "RUNNING")
                self.assertEqual(effects, [])


if __name__ == "__main__":
    unittest.main()
