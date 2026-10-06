from __future__ import annotations

import copy
import unittest

from test_sidecar_simple_watchdog import Owner, REGISTRATION, sample, watcher


def own_review(**fields):
    return sample(lineage={"registrationId": REGISTRATION, "intentId": "__previous_intent__"},
                  **fields)


DONE = '{"decision":"DONE","terminal":"SUPERVISOR_DONE"}'


class SimpleReviewLineageTests(unittest.TestCase):
    def test_manual_human_done_cannot_finish_prior_review_phase(self):
        owner = Owner([sample(), sample(user="human-u3", assistant="human-a3", version=9, text=DONE)])
        runtime = watcher(owner)
        self.assertEqual(runtime.step(), "transition_sent")
        self.assertEqual(runtime.step(), "need_input")
        self.assertEqual(runtime.diagnostics["reason"], "review_lineage_unverified")
        self.assertEqual(runtime.durable_state["status"], "RUNNING")
        self.assertEqual(len(owner.intents), 1)

    def test_matching_current_owned_review_can_finish(self):
        owner = Owner([sample(), own_review(user="review-u2", assistant="review-a2", version=8, text=DONE)])
        runtime = watcher(owner)
        checkpoints = []
        runtime.set_persistence_callback(lambda state: checkpoints.append(copy.deepcopy(state)))
        self.assertEqual(runtime.step(), "transition_sent")
        self.assertEqual(checkpoints[0]["expected_review_intent_id"], owner.intents[0]["intentId"])
        self.assertEqual(runtime.step(), "done")

    def test_lost_ack_restart_retains_expected_owned_review_link(self):
        owner = Owner([sample(), own_review(user="review-u2", assistant="review-a2", version=8, text=DONE)],
                      receipt=TimeoutError("receipt lost"))
        first = watcher(owner)
        checkpoints = []
        first.set_persistence_callback(lambda state: checkpoints.append(copy.deepcopy(state)))
        self.assertEqual(first.step(), "submission_unknown")
        self.assertEqual(checkpoints[0]["expected_review_intent_id"], owner.intents[0]["intentId"])
        restarted = watcher(owner)
        restarted.restore_state(checkpoints[0])
        self.assertEqual(restarted.step(), "done")
        self.assertEqual(len(owner.intents), 1)

    def test_unknown_review_link_stays_paused_across_restart_and_new_human_turn(self):
        owner = Owner([sample(), sample(user="u2", assistant="a2", version=8, text=DONE),
                       sample(user="human-u3", assistant="human-a3", version=9, text=DONE)])
        first = watcher(owner)
        self.assertEqual(first.step(), "transition_sent")
        self.assertEqual(first.step(), "need_input")
        restarted = watcher(owner)
        restarted.restore_state(first.durable_state)
        self.assertEqual(restarted.step(), "need_input")
        self.assertEqual(len(owner.intents), 1)

    def test_wrong_registration_or_intent_cannot_finish_review(self):
        for lineage in (
            {"registrationId": "20000000-0000-0000-0000-000000000002", "intentId": "__previous_intent__"},
            {"registrationId": REGISTRATION, "intentId": "unrelated-intent"},
        ):
            with self.subTest(lineage=lineage):
                owner = Owner([sample(), sample(user="u2", assistant="a2", version=8, text=DONE,
                                               lineage=lineage)])
                runtime = watcher(owner)
                self.assertEqual(runtime.step(), "transition_sent")
                self.assertEqual(runtime.step(), "need_input")
                self.assertEqual(len(owner.intents), 1)

    def test_review_recovery_reserves_new_causal_link_before_dispatch(self):
        active = own_review(user="review-u2", assistant="review-a2", version=8,
                            generating=True, terminal=False, body="incomplete")
        stopped = own_review(user="review-u2", assistant="review-a2", version=9,
                             generating=False, terminal=False, body="incomplete")
        done = own_review(user="recovery-u3", assistant="recovery-a3", version=10, text=DONE)
        owner = Owner([sample(), active, active, stopped, done])
        clock = [0.0]
        runtime = watcher(owner, clock=lambda: clock[0])
        checkpoints = []
        def checkpoint(state):
            self.assertEqual(len(owner.intents), len(checkpoints))
            checkpoints.append(copy.deepcopy(state))
        runtime.set_persistence_callback(checkpoint)
        self.assertEqual(runtime.step(), "transition_sent")
        self.assertEqual(runtime.step(), "active")
        clock[0] = 900
        self.assertEqual(runtime.step(), "liveness_recovery_sent")
        self.assertEqual([x["action"] for x in owner.intents], ["continue", "stop", "continue"])
        self.assertEqual(checkpoints[-1]["expected_review_intent_id"], owner.intents[-1]["intentId"])
        self.assertNotEqual(checkpoints[0]["expected_review_intent_id"], checkpoints[-1]["expected_review_intent_id"])
        self.assertEqual(runtime.step(), "done")

    def test_lost_recovery_ack_restart_keeps_new_review_link_and_never_replays(self):
        class LostRecoveryReceiptOwner(Owner):
            def submit(self, endpoint, payload):
                result = super().submit(endpoint, payload)
                if len(self.intents) == 3:
                    raise TimeoutError("recovery acknowledgment lost")
                return result
        active = own_review(user="review-u2", assistant="review-a2", version=8,
                            generating=True, terminal=False, body="incomplete")
        stopped = own_review(user="review-u2", assistant="review-a2", version=9,
                             generating=False, terminal=False, body="incomplete")
        owner = LostRecoveryReceiptOwner([
            sample(), active, active, stopped,
            own_review(user="recovery-u3", assistant="recovery-a3", version=10, text=DONE),
        ])
        clock = [0.0]
        first = watcher(owner, clock=lambda: clock[0])
        checkpoints = []
        first.set_persistence_callback(lambda state: checkpoints.append(copy.deepcopy(state)))
        self.assertEqual(first.step(), "transition_sent")
        self.assertEqual(first.step(), "active")
        clock[0] = 900
        self.assertEqual(first.step(), "liveness_recovery_unknown")
        self.assertEqual(checkpoints[-1]["expected_review_intent_id"], owner.intents[-1]["intentId"])
        restarted = watcher(owner, clock=lambda: clock[0])
        restarted.restore_state(checkpoints[-1])
        self.assertEqual(restarted.step(), "done")
        self.assertEqual(len(owner.intents), 3)

    def test_incomplete_review_reserves_recovery_lineage_without_stopping_or_phase_change(self):
        incomplete = own_review(user="review-u2", assistant="review-a2", version=8,
                                terminal=False, body="incomplete")
        checkpoints = []
        class CheckedOwner(Owner):
            def submit(self, endpoint, payload):
                self_case.assertEqual(checkpoints[-1]["expected_review_intent_id"],
                                      payload["intent"]["intentId"])
                return super().submit(endpoint, payload)
        self_case = self
        owner = CheckedOwner([sample(), incomplete, incomplete, incomplete,
                              own_review(user="recovery-u3", assistant="recovery-a3", version=9, text=DONE)])
        clock = [0.0]
        runtime = watcher(owner, clock=lambda: clock[0])
        runtime.set_persistence_callback(lambda state: checkpoints.append(copy.deepcopy(state)))
        self.assertEqual(runtime.step(), "transition_sent")
        original_review_id = runtime.durable_state["expected_review_intent_id"]
        self.assertEqual(runtime.step(), "blocked")
        clock[0] = 30
        self.assertEqual(runtime.step(), "liveness_recovery_sent")
        self.assertEqual([x["action"] for x in owner.intents], ["continue", "continue"])
        self.assertNotEqual(runtime.durable_state["expected_review_intent_id"], original_review_id)
        self.assertEqual(runtime.durable_state["phase"], 1)
        self.assertEqual(runtime.durable_state["cycle"], 0)
        self.assertIn("REVIEW", owner.intents[-1]["text"])
        self.assertEqual(runtime.step(), "done")

    def test_incomplete_review_lost_ack_preserves_new_link_across_restart(self):
        class LostReceiptOwner(Owner):
            def submit(self, endpoint, payload):
                result = super().submit(endpoint, payload)
                if len(self.intents) == 2:
                    raise TimeoutError("recovery receipt lost")
                return result
        incomplete = own_review(user="review-u2", assistant="review-a2", version=8,
                                terminal=False, body="incomplete")
        owner = LostReceiptOwner([sample(), incomplete, incomplete, incomplete,
                                  own_review(user="recovery-u3", assistant="recovery-a3", version=9, text=DONE)])
        clock = [0.0]
        runtime = watcher(owner, clock=lambda: clock[0])
        checkpoints = []
        runtime.set_persistence_callback(lambda state: checkpoints.append(copy.deepcopy(state)))
        self.assertEqual(runtime.step(), "transition_sent")
        self.assertEqual(runtime.step(), "blocked")
        clock[0] = 30
        self.assertEqual(runtime.step(), "liveness_recovery_unknown")
        self.assertEqual(checkpoints[-1]["expected_review_intent_id"], owner.intents[-1]["intentId"])
        restarted = watcher(owner, clock=lambda: clock[0])
        restarted.restore_state(checkpoints[-1])
        self.assertEqual(restarted.step(), "done")
        self.assertEqual(len(owner.intents), 2)

    def test_incomplete_review_reservation_failure_never_dispatches_or_changes_old_link(self):
        incomplete = own_review(user="review-u2", assistant="review-a2", version=8,
                                terminal=False, body="incomplete")
        owner = Owner([sample(), incomplete])
        clock = [0.0]
        runtime = watcher(owner, clock=lambda: clock[0])
        checkpoints = []
        runtime.set_persistence_callback(lambda state: checkpoints.append(copy.deepcopy(state)))
        self.assertEqual(runtime.step(), "transition_sent")
        original_review_id = runtime.durable_state["expected_review_intent_id"]
        def checkpoint(state):
            if state["expected_review_intent_id"] != original_review_id:
                raise OSError("review lineage checkpoint failed")
            checkpoints.append(copy.deepcopy(state))
        runtime.set_persistence_callback(checkpoint)
        self.assertEqual(runtime.step(), "blocked")
        clock[0] = 30
        self.assertEqual(runtime.step(), "liveness_recovery_rejected")
        self.assertEqual(runtime.durable_state["expected_review_intent_id"], original_review_id)
        clock[0] = 1000
        self.assertEqual(runtime.step(), "blocked")
        self.assertEqual(len(owner.intents), 1)
        self.assertEqual(checkpoints[-1]["expected_review_intent_id"], original_review_id)

    def test_storage_failure_before_liveness_dispatch_never_stops_or_refreshes(self):
        for fields in (
            {"generating": True, "terminal": False, "body": "incomplete"},
            {"generating": False, "terminal": False, "body": "incomplete"},
        ):
            with self.subTest(fields=fields):
                owner = Owner([sample(**fields)])
                clock = [0.0]
                runtime = watcher(owner, clock=lambda: clock[0])
                runtime.step()
                def fail(state):
                    raise OSError("durable store unavailable")
                runtime.set_persistence_callback(fail)
                clock[0] = 900
                self.assertEqual(runtime.step(), "liveness_storage_unavailable")
                self.assertEqual(owner.intents, [])

    def test_unresolved_legacy_review_fence_survives_checkpoint_and_restart(self):
        owner = Owner([sample(text=DONE)])
        first = watcher(owner)
        first.restore_state({
            "phase": 0, "cycle": 1, "next_prompt": "next ACTION", "status": "RUNNING",
        })
        self.assertEqual(first.step(), "transition_pending")
        persisted = copy.deepcopy(first.durable_state)
        restarted = watcher(owner)
        restarted.restore_state(persisted)
        self.assertEqual(restarted.step(), "transition_pending")
        self.assertEqual(owner.intents, [])

    def test_old_review_phase_without_durable_request_link_stays_paused(self):
        owner = Owner([sample(text=DONE, lineage={
            "registrationId": REGISTRATION, "intentId": "an-owned-but-unreserved-intent",
        })])
        runtime = watcher(owner)
        runtime.restore_state({"phase": 1, "cycle": 1, "next_prompt": "", "status": "RUNNING"})
        self.assertEqual(runtime.step(), "need_input")
        self.assertEqual(owner.intents, [])

    def test_phase_zero_human_reply_done_after_need_input_is_still_action(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                pending = sample(user="human-u3", assistant=None, text="", version=9,
                                 generating=False, terminal=False, body="empty", delivery="delivered")
                pending["state"]["progress"] = "unknown"
                owner = Owner([
                    sample(text="Need help.\n[SUPERVISOR_STATE: NEED_INPUT]"),
                    pending,
                    sample(user="human-u3", assistant="human-a3", version=10, text=DONE),
                ])
                runtime = watcher(owner)
                restored = {
                    "phase": 0, "cycle": 2, "next_prompt": "Previously reviewed ACTION",
                    "status": "RUNNING",
                }
                if not legacy:
                    restored["transition_turn_id"] = "prior-review-turn"
                runtime.restore_state(restored)
                self.assertEqual(runtime.step(), "need_input")
                self.assertEqual(runtime.step(), "human_input_observed")
                self.assertIn("transition_turn_id", runtime.durable_state)
                self.assertEqual(runtime.step(), "transition_sent")
                self.assertEqual(runtime.durable_state["phase"], 1)
                self.assertEqual(runtime.durable_state["status"], "RUNNING")
                self.assertIn("REVIEW 2", owner.intents[0]["text"])

    def test_unreadable_new_user_does_not_release_need_input_or_legacy_fence(self):
        owner = Owner([
            sample(text="Need help.\n[SUPERVISOR_STATE: NEED_INPUT]"),
            sample(user="unproven-u3", assistant="unproven-a3", version=9, readable=False),
        ])
        runtime = watcher(owner)
        runtime.restore_state({
            "phase": 0, "cycle": 2, "next_prompt": "Previously reviewed ACTION", "status": "RUNNING",
        })
        self.assertEqual(runtime.step(), "need_input")
        self.assertEqual(runtime.step(), "need_input")
        self.assertNotIn("transition_turn_id", runtime.durable_state)
        self.assertEqual(owner.intents, [])

    def test_unsettled_raw_delivery_does_not_release_need_input(self):
        for delivery in ("unknown", "none", "pending", "uncertain"):
            with self.subTest(delivery=delivery):
                pending = sample(user="human-u3", assistant=None, text="", version=9,
                                 generating=False, terminal=False, body="empty", delivery=delivery)
                pending["state"]["progress"] = "unknown"
                owner = Owner([sample(text="Need help.\n[SUPERVISOR_STATE: NEED_INPUT]"), pending])
                runtime = watcher(owner)
                runtime.restore_state({
                    "phase": 0, "cycle": 2, "next_prompt": "Previously reviewed ACTION", "status": "RUNNING",
                })
                self.assertEqual(runtime.step(), "need_input")
                self.assertEqual(runtime.step(), "need_input")
                self.assertNotIn("transition_turn_id", runtime.durable_state)
                self.assertEqual(owner.intents, [])

    def test_new_bind_phase_zero_can_supervise_human_action_done_as_action(self):
        owner = Owner([sample(user="human-u3", assistant="human-a3", version=9, text=DONE)])
        runtime = watcher(owner)
        self.assertEqual(runtime.step(), "transition_sent")
        self.assertEqual(runtime.durable_state["phase"], 1)
        self.assertEqual(runtime.durable_state["status"], "RUNNING")
        self.assertIn("REVIEW 0", owner.intents[0]["text"])


if __name__ == "__main__":
    unittest.main()
