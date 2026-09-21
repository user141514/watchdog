import unittest

from chat_watchdog.contracts import parse_conversation_state
from chat_watchdog.model import PageSnapshot, Phase
from chat_watchdog.state_client import StateProtocolError, StateUnavailable
from chat_watchdog.supervisor import Supervisor, StepResult

TARGET = "https://chatgpt.com/c/00000000-0000-0000-0000-000000000011"


def state(**overrides):
    value = {
        "contractVersion": 1,
        "conversationId": "conv-state",
        "target": TARGET,
        "stateVersion": 9,
        "turn": {
            "turnId": "turn-9",
            "userMessageId": "user-auth",
            "assistantMessageId": "assistant-auth",
        },
        "progress": "blocked",
        "body": "incomplete",
        "delivery": "delivered",
        "gate": "none",
        "writer": {"mode": "managed", "epoch": 3},
    }
    for key, nested in overrides.items():
        if key == "turn":
            value["turn"] = {**value["turn"], **nested}
        elif key == "writer":
            value["writer"] = {**value["writer"], **nested}
        else:
            value[key] = nested
    return parse_conversation_state(value)


class Page:
    target_url = TARGET

    def __init__(self, *, phase=Phase.FINISHED, text="partial task", user="relay-user", assistant="relay-assistant", snapshot_error=None):
        self.current = PageSnapshot(
            phase=phase,
            assistant_turn_id=assistant,
            assistant_text_signature="sig",
            assistant_text=text,
            assistant_count=4,
            user_count=4,
            user_turn_id=user,
        )
        self.effects = 0
        self.snapshot_error = snapshot_error

    def snapshot(self):
        if self.snapshot_error:
            raise self.snapshot_error
        return self.current

    def send_continue(self, *args):
        self.effects += 1
        raise AssertionError("managed Relay write")

    def retry_fault(self, *args):
        self.effects += 1
        raise AssertionError("managed Relay retry")


class Pool:
    calls = 0

    def try_acquire(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("managed recovery agent")


class Intents:
    def __init__(self, response=None):
        self.calls = []
        self.response = response or {"accepted": True}
        self.responses = list(response) if isinstance(response, list) else None

    def submit(self, *args, **kwargs):
        raise AssertionError("authoritative managed mode must not publish legacy intent payloads")

    def submit_v1(self, authoritative, text, *, source="watchdog", action="continue"):
        state = authoritative.to_dict()
        self.calls.append((
            state["target"],
            state["turn"]["userMessageId"],
            state["turn"]["assistantMessageId"],
            action,
        ))
        if self.responses is not None:
            return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return self.response


class ProgressStore:
    def __init__(self, *, stalled=False):
        self.stalled = stalled
        self.stall_settled = False
        self.observations = []
        self.claims = []
        self.settlements = []

    def observe(self, conversation_id, target_url, fingerprint, *, observable):
        self.observations.append((conversation_id, target_url, fingerprint, observable))
        return type("Pulse", (), {"progress_seq": 1, "last_progress_at": 1000.0})()

    def claim_stall(self, conversation_id, *, timeout_seconds, state_version, writer_epoch):
        self.claims.append((conversation_id, timeout_seconds, state_version, writer_epoch))
        return self.stalled and not self.stall_settled

    def settle_stall(self, conversation_id):
        self.settlements.append(conversation_id)
        self.stall_settled = True


class States:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def read(self, target):
        self.calls.append(target)
        if self.error:
            raise self.error
        return self.result


class AuthoritativeStateSupervisorTests(unittest.TestCase):
    def supervisor(self, authoritative, *, page=None, intent_response=None, progress_store=None):
        page = page or Page()
        intents = Intents(intent_response)
        pool = Pool()
        sup = Supervisor(
            page,
            pool,
            intent_client=intents,
            state_client=States(result=authoritative),
            progress_store=progress_store,
            progress_id="00000000-0000-0000-0000-000000000011" if progress_store else None,
            active_stall_seconds=300.0,
        )
        return sup, page, intents, pool

    def test_blocked_incomplete_uses_sidecar_identity_for_one_continuation(self):
        sup, page, intents, pool = self.supervisor(state())
        self.assertEqual(sup.step(), StepResult.CONTINUED)
        self.assertEqual(intents.calls, [(TARGET, "user-auth", "assistant-auth", "continue")])
        self.assertEqual(page.effects, 0)
        self.assertEqual(pool.calls, 0)
        self.assertEqual(sup.step(), StepResult.ALREADY_HANDLED)

    def test_sidecar_active_overrides_finished_relay_phase(self):
        sup, page, intents, _ = self.supervisor(state(progress="active", body="incomplete"))
        self.assertEqual(page.current.phase, Phase.FINISHED)
        self.assertEqual(sup.step(), StepResult.ACTIVE)
        self.assertEqual(intents.calls, [])

    def test_active_stall_uses_versioned_stop_once_without_relay_write(self):
        progress_store = ProgressStore(stalled=True)
        authoritative = state(
            progress="active",
            body="empty",
            turn={"assistantMessageId": None},
        )
        page = Page(phase=Phase.RESPONDING, user="user-auth", assistant="", text="Thinking")
        sup, page, intents, pool = self.supervisor(
            authoritative,
            page=page,
            progress_store=progress_store,
        )

        self.assertEqual(sup.step(), StepResult.RECOVERY_STARTED)
        self.assertEqual(intents.calls, [(TARGET, "user-auth", None, "stop")])
        self.assertEqual(page.effects, 0)
        self.assertEqual(pool.calls, 0)
        self.assertEqual(progress_store.claims, [
            ("00000000-0000-0000-0000-000000000011", 300.0, 9, 3)
        ])

        # Accepted stop settles this epoch; a repeated poll cannot issue another intent.
        self.assertEqual(progress_store.settlements, ["00000000-0000-0000-0000-000000000011"])
        self.assertEqual(sup.step(), StepResult.ACTIVE)
        self.assertEqual(len(intents.calls), 1)

    def test_uncertain_stop_retries_same_managed_intent_for_receipt_reconciliation(self):
        progress_store = ProgressStore(stalled=True)
        authoritative = state(
            progress="active",
            body="empty",
            turn={"assistantMessageId": None},
        )
        page = Page(phase=Phase.RESPONDING, user="user-auth", assistant="", text="Thinking")
        sup, page, intents, pool = self.supervisor(
            authoritative,
            page=page,
            intent_response=[
                {"accepted": False, "reason": "delivery_uncertain"},
                {"accepted": True},
            ],
            progress_store=progress_store,
        )

        self.assertEqual(sup.step(), StepResult.DELIVERY_UNCERTAIN)
        self.assertEqual(progress_store.settlements, [])
        self.assertEqual(sup.step(), StepResult.RECOVERY_STARTED)
        self.assertEqual(intents.calls, [
            (TARGET, "user-auth", None, "stop"),
            (TARGET, "user-auth", None, "stop"),
        ])
        self.assertEqual(progress_store.settlements, ["00000000-0000-0000-0000-000000000011"])
        self.assertEqual(sup.step(), StepResult.ACTIVE)
        self.assertEqual(len(intents.calls), 2)
        self.assertEqual(page.effects, 0)
        self.assertEqual(pool.calls, 0)

    def test_active_liveness_suspends_when_relay_snapshot_is_unavailable(self):
        progress_store = ProgressStore(stalled=True)
        authoritative = state(
            progress="active",
            body="empty",
            turn={"assistantMessageId": None},
        )
        page = Page(snapshot_error=RuntimeError("relay down"))
        sup, _, intents, _ = self.supervisor(
            authoritative,
            page=page,
            progress_store=progress_store,
        )

        self.assertEqual(sup.step(), StepResult.ACTIVE)
        self.assertEqual(intents.calls, [])
        self.assertEqual(progress_store.observations[-1][-1], False)
        self.assertEqual(progress_store.claims, [])

    def test_unknown_human_gate_and_uncertain_delivery_fail_closed(self):
        cases = [
            (state(progress="unknown", body="unknown"), StepResult.WAITING),
            (state(gate="human_required"), StepResult.NEED_INPUT),
            (state(delivery="uncertain", progress="unknown", body="unknown"), StepResult.DELIVERY_UNCERTAIN),
        ]
        for authoritative, expected in cases:
            with self.subTest(expected=expected):
                sup, _, intents, _ = self.supervisor(authoritative)
                self.assertEqual(sup.step(), expected)
                self.assertEqual(intents.calls, [])

    def test_state_owner_outage_waits_and_never_falls_back_to_relay(self):
        page, intents, pool = Page(), Intents(), Pool()
        sup = Supervisor(
            page,
            pool,
            intent_client=intents,
            state_client=States(error=StateUnavailable("sidecar down")),
        )
        self.assertEqual(sup.step(), StepResult.WAITING)
        self.assertEqual(intents.calls, [])
        self.assertEqual(page.effects, 0)
        self.assertEqual(pool.calls, 0)

    def test_state_protocol_corruption_blocks_and_never_falls_back(self):
        page, intents, pool = Page(), Intents(), Pool()
        sup = Supervisor(
            page,
            pool,
            intent_client=intents,
            state_client=States(error=StateProtocolError("bad contract")),
        )
        self.assertEqual(sup.step(), StepResult.BLOCKED)
        self.assertEqual(intents.calls, [])
        self.assertEqual(page.effects, 0)
        self.assertEqual(pool.calls, 0)

    def test_terminal_done_requires_relay_text_bound_to_authoritative_turn(self):
        matching_page = Page(user="user-auth", assistant="assistant-auth", text="complete\nSUPERVISOR_DONE")
        sup, _, intents, _ = self.supervisor(state(progress="terminal", body="substantive"), page=matching_page)
        self.assertEqual(sup.step(), StepResult.DONE)
        self.assertEqual(intents.calls, [])

        stale_page = Page(user="other-user", assistant="other-assistant", text="SUPERVISOR_DONE")
        sup, _, intents, _ = self.supervisor(state(progress="terminal", body="substantive"), page=stale_page)
        self.assertEqual(sup.step(), StepResult.WAITING)
        self.assertEqual(intents.calls, [])

    def test_terminal_without_done_marker_continues_from_authoritative_identity(self):
        page = Page(user="user-auth", assistant="assistant-auth", text="valid result but more work remains")
        sup, _, intents, _ = self.supervisor(state(progress="terminal", body="substantive"), page=page)
        self.assertEqual(sup.step(), StepResult.CONTINUED)
        self.assertEqual(intents.calls, [(TARGET, "user-auth", "assistant-auth", "continue")])

    def test_blocked_substantive_without_terminal_evidence_waits(self):
        sup, _, intents, _ = self.supervisor(state(progress="blocked", body="substantive"))
        self.assertEqual(sup.step(), StepResult.WAITING)
        self.assertEqual(intents.calls, [])

    def test_non_delivered_state_never_emits_continuation(self):
        for delivery in ("none", "pending"):
            with self.subTest(delivery=delivery):
                sup, _, intents, _ = self.supervisor(state(delivery=delivery))
                self.assertEqual(sup.step(), StepResult.WAITING)
                self.assertEqual(intents.calls, [])

    def test_legacy_writer_state_blocks_managed_watchdog(self):
        sup, _, intents, _ = self.supervisor(state(writer={"mode": "legacy"}))
        self.assertEqual(sup.step(), StepResult.BLOCKED)
        self.assertEqual(intents.calls, [])

    def test_relay_outage_does_not_override_authoritative_blocked_lifecycle(self):
        page = Page(snapshot_error=RuntimeError("relay down"))
        sup, _, intents, _ = self.supervisor(state(), page=page)
        self.assertEqual(sup.step(), StepResult.CONTINUED)
        self.assertEqual(intents.calls, [(TARGET, "user-auth", "assistant-auth", "continue")])

    def test_terminal_state_waits_when_relay_semantic_text_is_unavailable(self):
        page = Page(snapshot_error=RuntimeError("relay down"))
        sup, _, intents, _ = self.supervisor(state(progress="terminal", body="substantive"), page=page)
        self.assertEqual(sup.step(), StepResult.WAITING)
        self.assertEqual(intents.calls, [])

    def test_v1_state_race_denials_wait_for_reobservation_and_are_not_marked_handled(self):
        for reason in ("stale_state", "writer_epoch_mismatch", "state_delivery_uncertain", "state_not_continuable"):
            with self.subTest(reason=reason):
                sup, _, intents, _ = self.supervisor(
                    state(),
                    intent_response={"accepted": False, "reason": reason, "currentStateVersion": 10, "currentWriterEpoch": 3},
                )
                self.assertEqual(sup.step(), StepResult.WAITING)
                self.assertEqual(sup.step(), StepResult.WAITING)
                self.assertEqual(len(intents.calls), 2)

    def test_v1_current_effect_uncertainty_remains_delivery_uncertain(self):
        sup, _, intents, _ = self.supervisor(
            state(),
            intent_response={"accepted": False, "reason": "delivery_uncertain"},
        )
        self.assertEqual(sup.step(), StepResult.DELIVERY_UNCERTAIN)
        self.assertEqual(len(intents.calls), 1)
