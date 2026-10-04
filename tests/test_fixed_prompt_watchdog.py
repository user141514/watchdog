from __future__ import annotations

import copy
import unittest

from test_sidecar_simple_watchdog import Owner, REGISTRATION, sample, watcher


def fixed_watcher(owner, wall, **kwargs):
    runtime = watcher(owner, clock=lambda: 0.0, liveness_timeout_seconds=10000, **kwargs)
    runtime.wall_clock = lambda: wall[0]
    return runtime


def review_sample(text, **fields):
    return sample(text=text, lineage={"registrationId": REGISTRATION, "intentId": "owned-review"}, **fields)


def review_state(**extra):
    return {"phase": 1, "cycle": 0, "next_prompt": "", "status": "RUNNING",
            "transition_turn_id": None, "expected_review_intent_id": "owned-review", **extra}


class FixedPromptWatchdogTests(unittest.TestCase):
    def test_independent_900_second_deadline_survives_growing_text_and_next_slot(self):
        active = lambda text, version: sample(text=text, generating=True, terminal=False,
                                              body="incomplete", version=version)
        stopped = sample(text="latest growing text", terminal=False, body="incomplete", version=11)
        owner = Owner([active("one", 7), active("two", 8), active("three", 9),
                       active("latest growing text", 10), stopped])
        wall = [0.0]
        runtime = fixed_watcher(owner, wall)
        checkpoints = []
        runtime.set_persistence_callback(lambda state: checkpoints.append(copy.deepcopy(state)))
        self.assertEqual(runtime.step(), "active")
        self.assertEqual(runtime.durable_state.get("fixed_prompt_next_due_at"), 900)
        self.assertEqual(checkpoints[0]["fixed_prompt_next_due_at"], 900)
        wall[0] = 899
        self.assertEqual(runtime.step(), "active")
        self.assertEqual(owner.intents, [])
        wall[0] = 900
        self.assertEqual(runtime.step(), "fixed_prompt_sent")
        self.assertEqual([x["action"] for x in owner.intents], ["stop", "continue"])
        self.assertIn("15 分钟固定兜底", owner.intents[-1]["text"])
        self.assertIn("ACTION phase=0", owner.intents[-1]["text"])
        self.assertEqual(runtime.durable_state["phase"], 0)
        self.assertEqual(runtime.durable_state["fixed_prompt_next_due_at"], 1800)
        next_turn = sample(user="u2", assistant="a2", terminal=False, body="incomplete", version=12)
        owner.observations = [next_turn, next_turn]
        wall[0] = 1799
        self.assertEqual(runtime.step(), "blocked")
        self.assertEqual(len(owner.intents), 2)
        wall[0] = 1800
        self.assertEqual(runtime.step(), "fixed_prompt_sent")
        self.assertEqual([x["action"] for x in owner.intents], ["stop", "continue", "continue"])
        self.assertEqual(runtime.durable_state["fixed_prompt_next_due_at"], 2700)

    def test_restart_retains_epoch_deadline_and_long_downtime_consumes_only_one_slot(self):
        owner = Owner([sample(terminal=False, body="empty")])
        wall = [0.0]
        first = fixed_watcher(owner, wall)
        checkpoints = []
        first.set_persistence_callback(lambda state: checkpoints.append(copy.deepcopy(state)))
        first.step()
        restarted = fixed_watcher(owner, wall)
        restarted.restore_state(checkpoints[-1])
        wall[0] = 899
        self.assertEqual(restarted.step(), "blocked")
        self.assertEqual(owner.intents, [])
        wall[0] = 4501
        self.assertEqual(restarted.step(), "fixed_prompt_sent")
        self.assertEqual(restarted.durable_state["fixed_prompt_next_due_at"], 5400)
        self.assertEqual(len(owner.intents), 1)
        self.assertEqual(restarted.step(), "transition_pending")
        self.assertEqual(len(owner.intents), 1)

    def test_human_unknown_and_unsettled_delivery_keep_due_pending_without_effects(self):
        for changed in ({"gate": True}, {"readable": False}, {"delivery": "pending"},
                        {"delivery": "uncertain"}, {"generating": None}, {"terminal": None},
                        {"assistant": None}, {"text": "wait\n[SUPERVISOR_STATE: NEED_INPUT]"}):
            with self.subTest(changed=changed):
                owner = Owner([sample(**{"terminal": False, "body": "incomplete", **changed})])
                wall = [0.0]
                runtime = fixed_watcher(owner, wall)
                runtime.step()
                wall[0] = 900
                runtime.step()
                self.assertEqual(owner.intents, [])
                self.assertEqual(runtime.durable_state.get("fixed_prompt_next_due_at"), 900)
                self.assertTrue(runtime.diagnostics["fixed_prompt_due"])

    def test_owned_review_done_and_latched_done_preempt_overdue_timer(self):
        owner = Owner([review_sample('{"decision":"DONE","terminal":"SUPERVISOR_DONE"}')])
        wall = [900.0]
        runtime = fixed_watcher(owner, wall)
        runtime.restore_state(review_state(fixed_prompt_next_due_at=900))
        self.assertEqual(runtime.step(), "done")
        reads = owner.reads
        wall[0] = 1800
        self.assertEqual(runtime.step(), "done")
        self.assertEqual(owner.intents, [])
        self.assertEqual(owner.reads, reads)

    def test_consumed_stop_reservation_allows_only_matching_finished_review_done_after_restart(self):
        done = '{"decision":"DONE","terminal":"SUPERVISOR_DONE"}'
        cases = (
            (review_state(transition_turn_id="a1", fixed_prompt_next_due_at=1800), "done"),
            (review_state(transition_turn_id="a1", expected_review_intent_id="new-review",
                          fixed_prompt_next_due_at=1800), "transition_pending"),
            ({"phase": 0, "cycle": 0, "next_prompt": "", "status": "RUNNING",
              "transition_turn_id": "a1", "fixed_prompt_next_due_at": 1800}, "transition_pending"),
        )
        for durable, expected in cases:
            with self.subTest(durable=durable):
                owner = Owner([review_sample(done)])
                restarted = fixed_watcher(owner, [1800.0])
                restarted.restore_state(durable)
                self.assertEqual(restarted.step(), expected)
                self.assertEqual(owner.intents, [])
                self.assertEqual(restarted.durable_state["status"],
                                 "DONE" if expected == "done" else "RUNNING")
                self.assertEqual(restarted.durable_state["fixed_prompt_next_due_at"], 1800)

    def test_normal_finished_action_transition_has_priority_and_coalesces_due_slot(self):
        owner = Owner([sample(generating=True, terminal=False, body="incomplete"), sample()])
        wall = [0.0]
        runtime = fixed_watcher(owner, wall)
        runtime.step()
        wall[0] = 900
        self.assertEqual(runtime.step(), "transition_sent")
        self.assertEqual(len(owner.intents), 1)
        self.assertTrue(owner.intents[0]["text"].startswith("REVIEW 0"))
        self.assertNotIn("15 分钟固定兜底", owner.intents[0]["text"])
        self.assertEqual(runtime.durable_state["phase"], 1)
        self.assertEqual(runtime.durable_state["fixed_prompt_next_due_at"], 1800)

    def test_invalid_owned_review_uses_fixed_prompt_and_reserves_current_review_link(self):
        owner = Owner([review_sample("unfinished review")])
        wall = [900.0]
        runtime = fixed_watcher(owner, wall)
        runtime.restore_state(review_state(fixed_prompt_next_due_at=900))
        checkpoints = []
        runtime.set_persistence_callback(lambda state: checkpoints.append(copy.deepcopy(state)))
        self.assertEqual(runtime.step(), "fixed_prompt_sent")
        self.assertEqual(len(owner.intents), 1)
        self.assertIn("15 分钟固定兜底", owner.intents[0]["text"])
        self.assertIn("REVIEW phase=1", owner.intents[0]["text"])
        self.assertEqual(checkpoints[-1]["expected_review_intent_id"], owner.intents[0]["intentId"])
        self.assertEqual(runtime.durable_state["phase"], 1)
        self.assertEqual(runtime.durable_state["cycle"], 0)
        self.assertEqual(runtime.step(), "transition_pending")

    def test_wrong_review_lineage_and_old_consumed_turn_preempt_fixed_timer(self):
        for state in (review_state(expected_review_intent_id="unowned", fixed_prompt_next_due_at=900),
                      {"phase": 0, "cycle": 0, "next_prompt": "", "status": "RUNNING",
                       "transition_turn_id": "a1", "fixed_prompt_next_due_at": 900}):
            with self.subTest(state=state):
                owner = Owner([sample(generating=True, terminal=False, body="incomplete")])
                runtime = fixed_watcher(owner, [900.0])
                runtime.restore_state(state)
                runtime.step()
                self.assertEqual(owner.intents, [])
                self.assertEqual(runtime.durable_state["fixed_prompt_next_due_at"], 900)

    def test_legacy_unresolved_review_fence_preempts_fixed_timer_without_exact_turn_key(self):
        owner = Owner([sample(text='{"decision":"CONTINUE","next_prompt":"next"}',
                              terminal=False, body="incomplete")])
        runtime = fixed_watcher(owner, [900.0])
        runtime.restore_state({"phase": 0, "cycle": 1, "next_prompt": "next",
                               "status": "RUNNING", "fixed_prompt_next_due_at": 900})
        self.assertEqual(runtime.step(), "transition_pending")
        self.assertEqual(owner.intents, [])
        self.assertEqual(runtime.durable_state["fixed_prompt_next_due_at"], 900)

    def test_failed_slot_reservation_has_no_stop_or_continue_and_restores_deadline(self):
        owner = Owner([sample(generating=True, terminal=False, body="incomplete")])
        wall = [0.0]
        runtime = fixed_watcher(owner, wall)
        runtime.step()
        def fail(state):
            raise OSError("deadline reservation unavailable")
        runtime.set_persistence_callback(fail)
        wall[0] = 900
        self.assertEqual(runtime.step(), "fixed_prompt_storage_unavailable")
        self.assertEqual(owner.intents, [])
        self.assertIsNone(runtime.durable_state["transition_turn_id"])
        self.assertEqual(runtime.durable_state["fixed_prompt_next_due_at"], 900)

    def test_failed_initial_deadline_persistence_never_captures_or_writes(self):
        owner = Owner()
        runtime = fixed_watcher(owner, [0.0])
        runtime.set_persistence_callback(lambda state: (_ for _ in ()).throw(OSError("store down")))
        self.assertEqual(runtime.step(), "fixed_prompt_storage_unavailable")
        self.assertEqual(owner.reads, 0)
        self.assertEqual(owner.intents, [])

    def test_unknown_fixed_stop_fence_survives_restart_and_blocks_old_active_turn(self):
        owner = Owner([sample(generating=True, terminal=False, body="incomplete")],
                      receipt=TimeoutError("stop acknowledgment lost"))
        wall = [0.0]
        runtime = fixed_watcher(owner, wall)
        checkpoints = []
        runtime.set_persistence_callback(lambda state: checkpoints.append(copy.deepcopy(state)))
        runtime.step()
        wall[0] = 900
        self.assertEqual(runtime.step(), "fixed_prompt_unknown")
        self.assertEqual([x["action"] for x in owner.intents], ["stop"])
        restarted = fixed_watcher(owner, wall)
        restarted.restore_state(checkpoints[-1])
        wall[0] = 1800
        self.assertEqual(restarted.step(), "transition_pending")
        self.assertEqual(len(owner.intents), 1)
        self.assertEqual(restarted.durable_state["fixed_prompt_next_due_at"], 1800)

    def test_definitive_rejection_restores_fence_but_keeps_consumed_slot(self):
        owner = Owner([sample(terminal=False, body="empty")],
                      receipt={"accepted": False, "reason": "stale_state"})
        wall = [0.0]
        runtime = fixed_watcher(owner, wall)
        runtime.step()
        wall[0] = 900
        self.assertEqual(runtime.step(), "fixed_prompt_stale")
        self.assertIsNone(runtime.durable_state["transition_turn_id"])
        self.assertEqual(runtime.durable_state["fixed_prompt_next_due_at"], 1800)
        wall[0] = 901
        runtime.step()
        self.assertEqual(len(owner.intents), 1)

    def test_deadline_and_interval_validation_preserve_old_durable_state(self):
        runtime = fixed_watcher(Owner(), [0.0])
        old = {"phase": 0, "cycle": 0, "next_prompt": "", "status": "RUNNING"}
        runtime.restore_state(old)
        for value in (None, False, True, 0, -1, float("nan"), float("inf"), "900"):
            with self.subTest(deadline=value):
                with self.assertRaises(ValueError):
                    runtime.restore_state({**old, "fixed_prompt_next_due_at": value})
        for value in (False, True, 0, -1, float("nan"), float("inf"), "900"):
            with self.subTest(interval=value):
                with self.assertRaises(ValueError):
                    watcher(Owner(), fixed_prompt_interval_seconds=value)
