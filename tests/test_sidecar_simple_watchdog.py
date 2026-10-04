from __future__ import annotations

import copy
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from chat_watchdog.intent_client import SidecarIntentClient
from chat_watchdog.model import Phase
from chat_watchdog.observation_client import (
    ObservationProtocolError, ObservationUnavailable, SidecarObservationClient,
    SidecarObservationPage,
)
from chat_watchdog.simple_watchdog import SimpleWatcher
from chat_watchdog.supervisor import Supervisor, StepResult

TARGET = "https://chatgpt.com/c/00000000-0000-0000-0000-000000000123"
NOW = 1790899200.0
REGISTRATION = "10000000-0000-0000-0000-000000000001"
FIXTURE_REVIEW_INTENT = "fixture-review-intent"


def sample(*, text="action output", user="u1", assistant="a1", version=7,
           generating=False, terminal=True, body="substantive",
           gate=False, delivery="delivered", readable=True, observed_at=None, lineage=None):
    return {
        "found": True,
        "lineage": lineage or {"registrationId": None, "intentId": None},
        "state": {
            "contractVersion": 1, "conversationId": "conv-123", "target": TARGET,
            "stateVersion": version,
            "turn": {"turnId": "t1", "userMessageId": user, "assistantMessageId": assistant},
            "progress": "active" if generating else ("terminal" if terminal else "blocked"),
            "body": body, "delivery": "delivered",
            "gate": "human_required" if gate else "none",
            "writer": {"mode": "managed", "epoch": 4},
        },
        "observation": {
            "contractVersion": 1, "source": "browser", "conversationId": "conv-123",
            "target": TARGET,
            "observedAt": observed_at or datetime.fromtimestamp(NOW, timezone.utc).isoformat(),
            "turnId": "t1", "userMessageId": user, "assistantMessageId": assistant,
            "assistantText": text, "readable": readable, "generating": generating,
            "terminal": terminal, "body": body, "humanGate": gate,
            "delivery": delivery, "requestId": None,
        },
    }


class Owner:
    def __init__(self, observations=None, receipt=None):
        self.observations = list(observations or [sample()])
        self.last = self.observations[0]
        self.receipt = receipt or {"accepted": True, "messageId": "sent-user"}
        self.intents = []
        self.reads = 0

    def observe(self, endpoint, payload):
        self.reads += 1
        if self.observations:
            self.last = self.observations.pop(0)
        result = copy.deepcopy(self.last)
        if result.get("lineage", {}).get("intentId") == "__previous_intent__":
            sent_turns = [intent for intent in self.intents if intent.get("action") == "continue"]
            result["lineage"]["intentId"] = sent_turns[-1]["intentId"] if sent_turns else None
        return result

    def submit(self, endpoint, payload):
        self.intents.append(copy.deepcopy(payload.get("intent", payload)))
        if isinstance(self.receipt, Exception):
            raise self.receipt
        return copy.deepcopy(self.receipt)

    def clients(self):
        return (
            SidecarObservationClient(request_json=self.observe, clock=lambda: NOW),
            SidecarIntentClient(request_json=self.submit),
        )


def watcher(owner, **kwargs):
    observation, intent = owner.clients()
    return SimpleWatcher(TARGET, observation_client=observation, intent_client=intent,
                         registration_id=REGISTRATION, sleep=lambda _: None, **kwargs)


class ObservationContractTests(unittest.TestCase):
    def test_reads_fresh_browser_observation_and_same_sample_state(self):
        owner = Owner()
        value = owner.clients()[0].read(TARGET)
        self.assertEqual(value.state.to_dict()["stateVersion"], 7)
        self.assertEqual(value.observation.to_dict()["assistantText"], "action output")

    def test_state_only_reply_is_never_current_observation(self):
        owner = Owner([{"found": True, "state": sample()["state"]}])
        with self.assertRaises(ObservationProtocolError):
            owner.clients()[0].read(TARGET)

    def test_stale_future_and_identity_mismatch_samples_fail_closed(self):
        stale = sample(observed_at=datetime.fromtimestamp(NOW - 31, timezone.utc).isoformat())
        future = sample(observed_at=datetime.fromtimestamp(NOW + 31, timezone.utc).isoformat())
        wrong = sample()
        wrong["observation"]["assistantMessageId"] = "different-assistant"
        wrong_conversation = sample()
        wrong_conversation["observation"]["conversationId"] = "another-conversation"
        wrong_target = sample()
        wrong_target["observation"]["target"] = "https://chatgpt.com/c/00000000-0000-0000-0000-000000000999"
        for value in (stale, future, wrong, wrong_conversation, wrong_target):
            with self.subTest(value=value):
                with self.assertRaises((ObservationProtocolError, ObservationUnavailable)):
                    Owner([value]).clients()[0].read(TARGET)

    def test_persistent_adapter_binds_before_first_capture_and_getters_do_not_read(self):
        owner = Owner()
        observation, intent = owner.clients()
        events = []
        original_observe = observation._request
        def observe(endpoint, payload):
            events.append("capture")
            return original_observe(endpoint, payload)
        observation._request = observe
        def bind(registration_id, target):
            events.append("bind")
            return {"accepted": True, "registrationId": registration_id, "target": target}
        intent.bind_watch = bind
        page = SidecarObservationPage(TARGET, observation, intent, refresh_on_snapshot=True)
        self.assertEqual(page.current_url(), TARGET)
        self.assertIsNone(page.completion_text)
        self.assertEqual(events, [])
        page.bind_registration(REGISTRATION)
        self.assertEqual(page.snapshot().phase, Phase.FINISHED)
        self.assertEqual(events, ["bind", "capture"])
        self.assertEqual(page.read(TARGET).state_version, 7)
        self.assertEqual(events, ["bind", "capture"])

    def test_persistent_adapter_discards_previous_state_when_capture_fails(self):
        owner = Owner([sample(), {"found": False, "reason": "observation_unavailable"}])
        observation, intent = owner.clients()
        page = SidecarObservationPage(TARGET, observation, intent, refresh_on_snapshot=True)
        self.assertEqual(page.snapshot().phase, Phase.FINISHED)
        with self.assertRaises(ObservationUnavailable):
            page.snapshot()
        with self.assertRaises(ObservationUnavailable):
            page.read(TARGET)

    def test_nonlocal_endpoint_and_nonexact_target_are_rejected(self):
        with self.assertRaises(ValueError):
            SidecarObservationClient("https://remote.example/internal/conversation-observation")
        client = Owner().clients()[0]
        with self.assertRaises(ValueError):
            client.read("https://chatgpt.com/")

    def test_unknown_signals_and_gates_never_project_finished(self):
        for fields in ({"generating": None}, {"terminal": None}, {"gate": None},
                       {"delivery": "unknown"}, {"readable": False},
                       {"body": "unknown"}, {"body": "incomplete"}):
            with self.subTest(fields=fields):
                runtime = watcher(Owner([sample(**fields)]))
                self.assertEqual(runtime.step(), "blocked")
                self.assertEqual(runtime.durable_state["phase"], 0)


class ManagedSimpleWatcherTests(unittest.TestCase):
    def test_default_watcher_has_no_browser_attachment_even_when_owner_unavailable(self):
        with patch("chat_watchdog.simple_watchdog.RelayChatGPTPage.connect",
                   side_effect=AssertionError("Watchdog must not attach")):
            with patch("chat_watchdog.observation_client._post",
                       return_value={"found": False, "reason": "target_unavailable"}):
                runtime = SimpleWatcher(TARGET)
                self.assertEqual(runtime.step(), "observation_unavailable")
                self.assertEqual(runtime.diagnostics["reason"], "target_unavailable")

    def test_action_done_transitions_to_review_only_once_using_sample_version_epoch(self):
        owner = Owner([sample(text="SUPERVISOR_DONE")])
        runtime = watcher(owner)
        self.assertEqual(runtime.step(), "transition_sent")
        self.assertEqual(runtime.step(), "transition_pending")
        self.assertEqual(runtime.durable_state["phase"], 1)
        self.assertEqual(len(owner.intents), 1)
        intent = owner.intents[0]
        self.assertEqual(intent["action"], "continue")
        self.assertEqual(intent["expectedStateVersion"], 7)
        self.assertEqual(intent["expectedWriterEpoch"], 4)
        self.assertEqual(intent["expected"], {"userMessageId": "u1", "assistantMessageId": "a1"})
        self.assertIn("REVIEW 0", intent["text"])

    def test_review_continue_preserves_prompt_and_review_done_is_durable_noop(self):
        lineage = {"registrationId": REGISTRATION, "intentId": FIXTURE_REVIEW_INTENT}
        owner = Owner([sample(text='{"decision":"CONTINUE","next_prompt":"next concrete step"}', lineage=lineage)])
        runtime = watcher(owner, wall_clock=lambda: NOW)
        runtime.restore_state({"phase": 1, "cycle": 3, "next_prompt": "", "status": "RUNNING",
                               "expected_review_intent_id": FIXTURE_REVIEW_INTENT})
        self.assertEqual(runtime.step(), "transition_sent")
        self.assertEqual(runtime.durable_state, {
            "phase": 0, "cycle": 4, "next_prompt": "next concrete step", "status": "RUNNING",
            "transition_turn_id": "a1",
            "expected_review_intent_id": None, "liveness_attempted_for": None,
            "fixed_prompt_next_due_at": 1790900100.0,
        })
        self.assertEqual(owner.intents[0]["text"], "next concrete step")
        done_owner = Owner([sample(text='{"decision":"DONE","terminal":"SUPERVISOR_DONE"}', lineage=lineage)])
        done = watcher(done_owner)
        done.restore_state({"phase": 1, "cycle": 4, "next_prompt": "", "status": "RUNNING",
                            "expected_review_intent_id": FIXTURE_REVIEW_INTENT})
        self.assertEqual(done.step(), "done")
        self.assertEqual(done.step(), "done")
        self.assertEqual(done_owner.reads, 1)
        self.assertEqual(done_owner.intents, [])

    def test_phase_reservation_is_durable_before_owner_dispatch(self):
        owner = Owner()
        runtime = watcher(owner, wall_clock=lambda: NOW)
        checkpoints = []
        def persist(state):
            self.assertEqual(owner.intents, [])
            self.assertEqual(owner.reads, 0 if state["phase"] == 0 else 1)
            checkpoints.append(copy.deepcopy(state))
        runtime.set_persistence_callback(persist)
        self.assertEqual(runtime.step(), "transition_sent")
        self.assertEqual(len(checkpoints), 2)
        self.assertEqual(checkpoints[0], {
            "phase": 0, "cycle": 0, "next_prompt": "", "status": "RUNNING",
            "transition_turn_id": None, "expected_review_intent_id": None,
            "liveness_attempted_for": None, "fixed_prompt_next_due_at": 1790900100.0,
        })
        reservations = [state for state in checkpoints if state["phase"] == 1]
        self.assertEqual(len(reservations), 1)
        self.assertEqual(reservations[0]["transition_turn_id"], "a1")
        self.assertEqual(reservations[0]["expected_review_intent_id"], owner.intents[0]["intentId"])
        self.assertEqual(reservations[0]["fixed_prompt_next_due_at"], 1790900100.0)

    def test_storage_failure_before_dispatch_never_reaches_owner(self):
        owner = Owner()
        runtime = watcher(owner)
        def unavailable(state):
            raise OSError("store unavailable")
        runtime.set_persistence_callback(unavailable)
        self.assertEqual(runtime.step(), "fixed_prompt_storage_unavailable")
        self.assertEqual(owner.reads, 0)
        self.assertEqual(owner.intents, [])
        self.assertEqual(runtime.durable_state["phase"], 0)
        self.assertIsNone(runtime.durable_state["transition_turn_id"])
        self.assertIsNone(runtime.durable_state["expected_review_intent_id"])
        self.assertNotIn("fixed_prompt_next_due_at", runtime.durable_state)

        transition_owner = Owner()
        transition = watcher(transition_owner, wall_clock=lambda: NOW)
        checkpoints = []
        def fail_reservation(state):
            if state["phase"] == 1:
                self.assertEqual(transition_owner.intents, [])
                raise OSError("transition store unavailable")
            checkpoints.append(copy.deepcopy(state))
        transition.set_persistence_callback(fail_reservation)
        self.assertEqual(transition.step(), "transition_storage_unavailable")
        self.assertEqual(transition_owner.reads, 1)
        self.assertEqual(transition_owner.intents, [])
        self.assertEqual(checkpoints, [{
            "phase": 0, "cycle": 0, "next_prompt": "", "status": "RUNNING",
            "transition_turn_id": None, "expected_review_intent_id": None,
            "liveness_attempted_for": None, "fixed_prompt_next_due_at": 1790900100.0,
        }])

    def test_supervisor_cannot_write_using_retained_terminal_with_unknown_fresh_observation(self):
        owner = Owner([sample(generating=None)])
        observation, intent = owner.clients()
        page = SidecarObservationPage(TARGET, observation, intent, registration_id=REGISTRATION)
        runtime = Supervisor(page, None, intent_client=page, state_client=page)
        self.assertEqual(runtime.step(), StepResult.BLOCKED)
        self.assertEqual(owner.intents, [])

    def test_new_action_json_still_transitions_to_review_after_prior_review(self):
        owner = Owner([
            sample(text='{"decision":"CONTINUE","next_prompt":"advance next step"}',
                   lineage={"registrationId": REGISTRATION, "intentId": FIXTURE_REVIEW_INTENT}),
            sample(assistant="a2", user="u2", version=8,
                   text='{"decision":"DONE","terminal":"SUPERVISOR_DONE"}'),
        ])
        runtime = watcher(owner)
        runtime.restore_state({"phase": 1, "cycle": 0, "next_prompt": "", "status": "RUNNING",
                               "expected_review_intent_id": FIXTURE_REVIEW_INTENT})
        self.assertEqual(runtime.step(), "transition_sent")
        self.assertEqual(runtime.step(), "transition_sent")
        self.assertEqual(runtime.durable_state["phase"], 1)
        self.assertEqual(len(owner.intents), 2)
        self.assertIn("REVIEW 1", owner.intents[1]["text"])

    def test_incomplete_waits_for_stability_then_continues_once_using_fresh_cas(self):
        for configured_timeout, recovery_at in ((900, 30), (10, 10)):
            with self.subTest(timeout=configured_timeout):
                incomplete = sample(terminal=False, body="incomplete")
                fresh = sample(terminal=False, body="incomplete", version=9)
                fresh["state"]["writer"]["epoch"] = 6
                owner = Owner([incomplete, incomplete, incomplete, fresh])
                clock = [0.0]
                runtime = watcher(owner, clock=lambda: clock[0],
                                  liveness_timeout_seconds=configured_timeout)
                checkpoints = []
                runtime.set_persistence_callback(lambda state: checkpoints.append(copy.deepcopy(state)))
                self.assertEqual(runtime.step(), "blocked")
                clock[0] = recovery_at - 1
                self.assertEqual(runtime.step(), "blocked")
                self.assertEqual(owner.intents, [])
                clock[0] = recovery_at
                self.assertEqual(runtime.step(), "liveness_recovery_sent")
                self.assertEqual(len(owner.intents), 1)
                sent = owner.intents[0]
                self.assertEqual(sent["action"], "continue")
                self.assertEqual(sent["expectedStateVersion"], 9)
                self.assertEqual(sent["expectedWriterEpoch"], 6)
                self.assertEqual(sent["expected"], {"userMessageId": "u1", "assistantMessageId": "a1"})
                self.assertIn("ACTION phase=0", sent["text"])
                self.assertEqual(checkpoints[-1]["phase"], 0)
                self.assertEqual(checkpoints[-1]["cycle"], 0)
                clock[0] += 900
                self.assertEqual(runtime.step(), "blocked")
                self.assertEqual(len(owner.intents), 1)

    def test_incomplete_stability_starts_when_active_or_empty_blocked_becomes_incomplete(self):
        for previous_kind, text in (("active", "partial work"), ("empty", "")):
            with self.subTest(previous=previous_kind):
                previous = sample(text=text, terminal=False,
                                  generating=previous_kind == "active",
                                  body="incomplete" if previous_kind == "active" else "empty")
                incomplete = sample(text=text, terminal=False, body="incomplete")
                owner = Owner([previous, incomplete])
                clock = [0.0]
                runtime = watcher(owner, clock=lambda: clock[0])
                self.assertEqual(runtime.step(), "active" if previous_kind == "active" else "blocked")
                clock[0] = 60
                self.assertEqual(runtime.step(), "blocked")
                self.assertEqual(owner.intents, [])
                clock[0] = 89
                self.assertEqual(runtime.step(), "blocked")
                self.assertEqual(owner.intents, [])
                clock[0] = 90
                self.assertEqual(runtime.step(), "liveness_recovery_sent")
                self.assertEqual(len(owner.intents), 1)
                self.assertEqual(owner.intents[0]["action"], "continue")

    def test_incomplete_lost_receipt_restart_does_not_repeat_the_same_recovery(self):
        owner = Owner([sample(terminal=False, body="incomplete")],
                      receipt=TimeoutError("continue acknowledgment lost"))
        clock = [0.0]
        first = watcher(owner, clock=lambda: clock[0])
        checkpoints = []
        first.set_persistence_callback(lambda state: checkpoints.append(copy.deepcopy(state)))
        self.assertEqual(first.step(), "blocked")
        clock[0] = 30
        self.assertEqual(first.step(), "liveness_recovery_unknown")
        restarted = watcher(owner, clock=lambda: clock[0])
        restarted.restore_state(checkpoints[-1])
        self.assertEqual(restarted.step(), "blocked")
        clock[0] = 1000
        self.assertEqual(restarted.step(), "blocked")
        self.assertEqual(len(owner.intents), 1)

    def test_incomplete_recovery_rejects_changed_fresh_observation_without_dispatch(self):
        changes = (
            {"text": "new assistant progress"},
            {"user": "new-human"},
            {"generating": True},
            {"terminal": True, "body": "substantive"},
            {"gate": True},
            {"delivery": "uncertain"},
            {"body": "unknown"},
            {"readable": False},
        )
        for fields in changes:
            with self.subTest(fields=fields):
                before = sample(terminal=False, body="incomplete")
                changed = sample(**{"terminal": False, "body": "incomplete", **fields})
                owner = Owner([before, changed])
                observation, intent = owner.clients()
                page = SidecarObservationPage(TARGET, observation, intent, registration_id=REGISTRATION)
                result = page.recover_incomplete("continue exact action", page.snapshot().turn_key)
                self.assertFalse(result.accepted)
                self.assertEqual(owner.intents, [])

    def test_blocked_liveness_refresh_uses_owner_scope_after_900s_only_once(self):
        clock = [0.0]
        owner = Owner([sample(terminal=False, body="empty")])
        runtime = watcher(owner, clock=lambda: clock[0])
        self.assertEqual(runtime.step(), "blocked")
        clock[0] = 899
        self.assertEqual(runtime.step(), "blocked")
        self.assertEqual(owner.intents, [])
        clock[0] = 900
        self.assertEqual(runtime.step(), "blocked_recovery_refresh")
        self.assertEqual(owner.intents[0]["kind"], "refresh")
        self.assertEqual(owner.intents[0]["registrationId"], REGISTRATION)
        self.assertEqual(owner.intents[0]["expectedStateVersion"], 7)
        runtime.step()
        self.assertEqual(len(owner.intents), 1)

    def test_retained_progress_never_authorizes_write_against_current_observation(self):
        # Cross product: only the positive active/terminal/blocked agreement
        # can be used for effects; an old state's classification grants none.
        raw_cases = [
            {"generating": True, "terminal": False, "body": "incomplete", "progress": "active"},
            {"generating": False, "terminal": True, "body": "substantive", "progress": "terminal"},
            {"generating": False, "terminal": False, "body": "incomplete", "progress": "blocked"},
        ]
        for raw in raw_cases:
            for retained in ("active", "terminal", "blocked", "idle", "unknown"):
                if retained == raw["progress"]:
                    continue
                with self.subTest(raw=raw, retained=retained):
                    value = sample(generating=raw["generating"], terminal=raw["terminal"],
                                   body=raw["body"])
                    value["state"]["progress"] = retained
                    classic_owner = Owner([value])
                    observation, intent = classic_owner.clients()
                    page = SidecarObservationPage(TARGET, observation, intent,
                                                  registration_id=REGISTRATION)
                    classic = Supervisor(page, None, intent_client=page, state_client=page)
                    self.assertEqual(classic.step(), StepResult.BLOCKED)
                    self.assertEqual(classic_owner.intents, [])

                    simple_owner = Owner([value])
                    clock = [0.0]
                    simple = watcher(simple_owner, clock=lambda: clock[0], wall_clock=lambda: NOW + clock[0])
                    checkpoints = []
                    simple.set_persistence_callback(checkpoints.append)
                    self.assertEqual(simple.step(), "blocked")
                    clock[0] = 900
                    self.assertEqual(simple.step(), "blocked")
                    self.assertEqual(simple_owner.intents, [])
                    self.assertEqual(checkpoints, [{
                        "phase": 0, "cycle": 0, "next_prompt": "", "status": "RUNNING",
                        "transition_turn_id": None, "expected_review_intent_id": None,
                        "liveness_attempted_for": None, "fixed_prompt_next_due_at": 1790900100.0,
                    }])

    def test_pending_uncertain_or_human_gate_cannot_use_retained_blocked_authority(self):
        for changed in (
            {"delivery": "pending"}, {"delivery": "uncertain"},
            {"gate": True}, {"readable": False}, {"body": "unknown"},
            {"generating": True, "terminal": True},
        ):
            with self.subTest(changed=changed):
                value = sample(**{"terminal": False, "body": "incomplete", **changed})
                value["state"].update(progress="blocked", body="incomplete", gate="none")
                classic_owner = Owner([value])
                observation, intent = classic_owner.clients()
                page = SidecarObservationPage(TARGET, observation, intent,
                                              registration_id=REGISTRATION)
                classic = Supervisor(page, None, intent_client=page, state_client=page)
                self.assertEqual(classic.step(), StepResult.BLOCKED)
                self.assertEqual(classic_owner.intents, [])
                simple_owner = Owner([value])
                clock = [0.0]
                simple = watcher(simple_owner, clock=lambda: clock[0], wall_clock=lambda: NOW + clock[0])
                checkpoints = []
                simple.set_persistence_callback(checkpoints.append)
                simple.step()
                clock[0] = 900
                simple.step()
                self.assertEqual(simple_owner.intents, [])
                self.assertEqual(checkpoints, [{
                    "phase": 0, "cycle": 0, "next_prompt": "", "status": "RUNNING",
                    "transition_turn_id": None, "expected_review_intent_id": None,
                    "liveness_attempted_for": None, "fixed_prompt_next_due_at": 1790900100.0,
                }])

    def test_authoritative_human_gate_returns_pause_without_writes(self):
        value = sample(gate=True, terminal=False, body="incomplete")
        owner = Owner([value])
        observation, intent = owner.clients()
        page = SidecarObservationPage(TARGET, observation, intent, registration_id=REGISTRATION)
        classic = Supervisor(page, None, intent_client=page, state_client=page)
        self.assertEqual(classic.step(), StepResult.NEED_INPUT)
        self.assertEqual(owner.intents, [])

    def test_restart_never_interprets_consumed_action_json_as_review_done(self):
        owner = Owner([sample(text='{"decision":"DONE","terminal":"SUPERVISOR_DONE"}')],
                      receipt=TimeoutError("lost acknowledgment"))
        first = watcher(owner)
        self.assertEqual(first.step(), "submission_unknown")
        restarted = watcher(owner)
        restarted.restore_state(first.durable_state)
        self.assertEqual(restarted.step(), "transition_pending")
        self.assertEqual(len(owner.intents), 1)

    def test_transport_uncertainty_consumes_transition_before_next_tick(self):
        owner = Owner(receipt=TimeoutError("lost acknowledgment"))
        runtime = watcher(owner)
        self.assertEqual(runtime.step(), "submission_unknown")
        self.assertEqual(runtime.step(), "transition_pending")
        self.assertEqual(len(owner.intents), 1)

    def test_need_input_releases_only_after_fresh_new_human_turn_and_never_sends_on_release(self):
        owner = Owner([
            sample(text="operator required\n[SUPERVISOR_STATE: NEED_INPUT]"),
            sample(text="operator required\n[SUPERVISOR_STATE: NEED_INPUT]"),
            sample(user="u2", assistant=None, terminal=False, body="empty", delivery="delivered"),
            sample(user="u2", assistant="a2", text="human request handled"),
        ])
        runtime = watcher(owner)
        self.assertEqual(runtime.step(), "need_input")
        self.assertEqual(runtime.step(), "need_input")
        self.assertEqual(runtime.step(), "human_input_observed")
        self.assertEqual(owner.intents, [])
        self.assertEqual(runtime.step(), "transition_sent")

    def test_active_liveness_uses_stop_then_fresh_state_bound_recovery_continue_once(self):
        clock = [0.0]
        active = sample(generating=True, terminal=False, body="incomplete")
        stopped = sample(generating=False, terminal=False, body="incomplete", version=8)
        owner = Owner([active, active, stopped, stopped])
        runtime = watcher(owner, clock=lambda: clock[0], liveness_timeout_seconds=900)
        self.assertEqual(runtime.step(), "active")
        clock[0] = 900
        self.assertEqual(runtime.step(), "liveness_recovery_sent")
        self.assertEqual([x["action"] for x in owner.intents], ["stop", "continue"])
        self.assertEqual(owner.intents[1]["expectedStateVersion"], 8)
        self.assertIn("ACTION phase=0", owner.intents[1]["text"])
        runtime.step()
        self.assertEqual(len(owner.intents), 2)

    def test_stop_ack_without_fresh_stopped_observation_never_sends_recovery_prompt(self):
        clock = [0.0]
        active = sample(generating=True, terminal=False, body="incomplete")
        owner = Owner([active, active])
        runtime = watcher(owner, clock=lambda: clock[0], liveness_timeout_seconds=900,
                          submission_confirm_seconds=0)
        self.assertEqual(runtime.step(), "active")
        clock[0] = 900
        self.assertEqual(runtime.step(), "liveness_recovery_rejected")
        self.assertEqual([x["action"] for x in owner.intents], ["stop"])
        runtime.step()
        self.assertEqual(len(owner.intents), 1)


class FixedPromptAdapterTests(unittest.TestCase):
    def page(self, owner):
        observation, intent = owner.clients()
        return SidecarObservationPage(TARGET, observation, intent, registration_id=REGISTRATION)

    def test_growing_active_turn_is_stopped_then_continued_with_fresh_state(self):
        active = sample(generating=True, terminal=False, body="incomplete")
        growing = sample(text="additional current work", generating=True, terminal=False,
                         body="incomplete", version=8)
        stopped = sample(text="latest current work", terminal=False, body="incomplete", version=9)
        stopped["state"]["writer"]["epoch"] = 6
        owner = Owner([active, growing, stopped])
        page = self.page(owner)
        reservations = []
        delivery = page.send_fixed_prompt(
            "fixed ACTION continuation", page.snapshot().turn_key,
            before_continue=reservations.append,
        )
        self.assertTrue(delivery.accepted)
        self.assertEqual([x["action"] for x in owner.intents], ["stop", "continue"])
        self.assertEqual(owner.intents[0]["expectedStateVersion"], 8)
        self.assertEqual(owner.intents[1]["expectedStateVersion"], 9)
        self.assertEqual(owner.intents[1]["expectedWriterEpoch"], 6)
        self.assertEqual(owner.intents[1]["expected"], {"userMessageId": "u1", "assistantMessageId": "a1"})
        self.assertEqual(reservations, [owner.intents[1]["intentId"]])

    def test_uncertain_stop_never_dispatches_continuation(self):
        active = sample(generating=True, terminal=False, body="incomplete")
        owner = Owner([active, active], receipt=TimeoutError("stop acknowledgment lost"))
        page = self.page(owner)
        delivery = page.send_fixed_prompt("fixed continuation", page.snapshot().turn_key)
        self.assertTrue(delivery.uncertain)
        self.assertEqual([x["action"] for x in owner.intents], ["stop"])

    def test_stop_ack_requires_observed_stop_before_continuation(self):
        active = sample(generating=True, terminal=False, body="incomplete")
        owner = Owner([active, active, active])
        page = self.page(owner)
        delivery = page.send_fixed_prompt("fixed continuation", page.snapshot().turn_key, stop_timeout=0)
        self.assertFalse(delivery.accepted)
        self.assertEqual([x["action"] for x in owner.intents], ["stop"])

    def test_changed_identity_and_untrusted_stopped_samples_prevent_continuation(self):
        fields = [
            {"user": "other-user"}, {"assistant": "other-assistant"}, {"gate": True},
            {"delivery": "uncertain"}, {"readable": False}, {"body": "unknown"},
        ]
        for changes in fields + [{"changed_turn": True}]:
            with self.subTest(changes=changes):
                active = sample(generating=True, terminal=False, body="incomplete")
                changed_turn = changes.get("changed_turn")
                changed = sample(**{"terminal": False, "body": "incomplete",
                                    **{k: v for k, v in changes.items() if k != "changed_turn"}})
                if changed_turn:
                    changed["state"]["turn"]["turnId"] = "other-turn"
                    changed["observation"]["turnId"] = "other-turn"
                owner = Owner([active, active, changed])
                page = self.page(owner)
                delivery = page.send_fixed_prompt("fixed continuation", page.snapshot().turn_key)
                self.assertFalse(delivery.accepted)
                self.assertEqual([x["action"] for x in owner.intents], ["stop"])

    def test_natural_finality_before_or_after_stop_wins_over_fixed_prompt(self):
        active = sample(generating=True, terminal=False, body="incomplete")
        done = sample(text='{"decision":"DONE","terminal":"SUPERVISOR_DONE"}', version=9)
        for observations, actions in (([active, done], []), ([active, active, done], ["stop"])):
            with self.subTest(actions=actions):
                owner = Owner(observations)
                page = self.page(owner)
                delivery = page.send_fixed_prompt("fixed REVIEW continuation", page.snapshot().turn_key)
                self.assertFalse(delivery.accepted)
                self.assertEqual([x["action"] for x in owner.intents], actions)

    def test_stopped_nonterminal_fixed_prompt_needs_no_fault_or_stagnant_signature(self):
        initial = sample(terminal=False, body="empty", text="")
        fresh = sample(terminal=False, body="incomplete", text="new partial output", version=9)
        owner = Owner([initial, fresh])
        page = self.page(owner)
        delivery = page.send_fixed_prompt("fixed continuation", page.snapshot().turn_key)
        self.assertTrue(delivery.accepted)
        self.assertEqual([x["action"] for x in owner.intents], ["continue"])
        self.assertEqual(owner.intents[0]["expectedStateVersion"], 9)

    def test_review_reservation_failure_prevents_continuation_after_stop(self):
        active = sample(generating=True, terminal=False, body="incomplete")
        stopped = sample(terminal=False, body="incomplete", version=9)
        owner = Owner([active, active, stopped])
        page = self.page(owner)
        def fail(_):
            raise OSError("durable review reservation unavailable")
        delivery = page.send_fixed_prompt("fixed REVIEW", page.snapshot().turn_key, before_continue=fail)
        self.assertFalse(delivery.accepted)
        self.assertEqual([x["action"] for x in owner.intents], ["stop"])

    def test_fresh_lineage_change_prevents_even_stop(self):
        active = sample(generating=True, terminal=False, body="incomplete",
                        lineage={"registrationId": REGISTRATION, "intentId": FIXTURE_REVIEW_INTENT})
        changed = sample(generating=True, terminal=False, body="incomplete",
                         lineage={"registrationId": REGISTRATION, "intentId": "different-intent"})
        owner = Owner([active, changed])
        page = self.page(owner)
        delivery = page.send_fixed_prompt("fixed REVIEW", page.snapshot().turn_key)
        self.assertFalse(delivery.accepted)
        self.assertEqual(owner.intents, [])


if __name__ == "__main__":
    unittest.main()
