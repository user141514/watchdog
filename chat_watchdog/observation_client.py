"""Fresh Sidecar browser observations projected into the simple Watchdog view.

The projection grants no browser capability. Every mutation is submitted to the
owner with the state version, writer epoch, and identities from the same sample.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .contracts import ContractValue, parse_conversation_state, parse_observation
from .model import PageSnapshot, Phase, PromptDelivery
from .state_client import _conversation_id, _validate_owner

DEFAULT_OBSERVATION_URL = "http://127.0.0.1:7337/internal/conversation-observation"
MAX_OBSERVATION_AGE_SECONDS = 30.0


class ObservationUnavailable(RuntimeError):
    """The owner cannot currently capture this exact conversation."""


class ObservationProtocolError(RuntimeError):
    """The captured sample violates the observation/state contract."""


def _post(endpoint, payload):
    request = Request(endpoint, data=json.dumps(payload).encode("utf-8"),
                      headers={"content-type": "application/json"}, method="POST")
    with urlopen(request, timeout=5.0) as response:
        if response.status != 200:
            raise ObservationUnavailable(f"observation owner returned HTTP {response.status}")
        return json.loads(response.read().decode("utf-8"))


def sibling_observation_endpoint(intent_endpoint: str) -> str:
    _validate_owner(intent_endpoint, "/internal/conversation-intents")
    url = urlsplit(intent_endpoint)
    return urlunsplit((url.scheme, url.netloc, "/internal/conversation-observation", "", ""))


@dataclass(frozen=True)
class ObservationSample:
    state: ContractValue
    observation: ContractValue
    lineage: dict[str, str | None] | None = None

    def _trusted_phase(self) -> tuple[Phase, str]:
        state, obs = self.state.to_dict(), self.observation.to_dict()
        if (not obs["readable"] or obs["generating"] is None or obs["terminal"] is None
                or obs["humanGate"] is None or obs["body"] == "unknown"
                or obs["delivery"] == "unknown"):
            return Phase.BLOCKED, "observation_signals_unknown"
        if obs["humanGate"] is True or state["gate"] != "none":
            return Phase.BLOCKED, "human_required"
        if obs["delivery"] != "delivered" or state["delivery"] != "delivered":
            return Phase.BLOCKED, "observation_delivery_unsettled"
        if not obs["userMessageId"] or not obs["assistantMessageId"]:
            return Phase.BLOCKED, "observation_identity_incomplete"
        # This complete positive mapping is shared by both Watchdog modes.
        # Retained owner state is usable only when it agrees with this actual
        # browser sample; unknown/idle state cannot authorize an effect.
        phases = {
            (True, False): ("active", Phase.RESPONDING),
            (False, True): ("terminal", Phase.FINISHED),
            (False, False): ("blocked", Phase.BLOCKED),
        }
        expected = phases.get((obs["generating"], obs["terminal"]))
        if expected is None:
            return Phase.BLOCKED, "observation_finality_conflict"
        progress, phase = expected
        if state["progress"] != progress:
            return Phase.BLOCKED, "observation_progress_mismatch"
        if state["body"] != obs["body"]:
            return Phase.BLOCKED, "observation_body_mismatch"
        if phase is Phase.FINISHED and obs["body"] != "substantive":
            return Phase.BLOCKED, "observation_final_body_unconfirmed"
        return phase, ""

    def snapshot(self) -> PageSnapshot:
        state, obs = self.state.to_dict(), self.observation.to_dict()
        text = (obs["assistantText"] or "") if obs["readable"] else ""
        phase, reason = self._trusted_phase()
        pending = obs["delivery"] == "pending" or state["delivery"] == "pending"
        pending = pending or not obs["assistantMessageId"]
        signature = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""
        return PageSnapshot(
            phase=phase, assistant_turn_id=obs["assistantMessageId"] or "",
            assistant_text_signature=signature, assistant_text=text,
            assistant_count=int(bool(obs["assistantMessageId"])),
            user_count=int(bool(obs["userMessageId"])),
            user_turn_id=obs["userMessageId"] or "", user_turn_pending=pending,
            interaction_required=bool(reason), stop_visible=obs["generating"] is True,
            fault_text=reason,
        )


class SidecarObservationClient:
    def __init__(self, endpoint=DEFAULT_OBSERVATION_URL, *, request_json=None,
                 clock=time.time, max_age_seconds=MAX_OBSERVATION_AGE_SECONDS):
        self.endpoint = _validate_owner(endpoint, "/internal/conversation-observation")
        self._request = request_json or _post
        self._clock = clock
        if max_age_seconds <= 0:
            raise ValueError("observation freshness interval must be positive")
        self.max_age_seconds = max_age_seconds

    def read(self, target: str) -> ObservationSample:
        expected_id = _conversation_id(target)
        if expected_id is None:
            raise ValueError("exact conversation target is required")
        try:
            payload = self._request(self.endpoint, {"target": target})
        except (ObservationUnavailable, ObservationProtocolError):
            raise
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ObservationProtocolError("invalid JSON from observation owner") from error
        except Exception as error:
            raise ObservationUnavailable(str(error)) from error
        if not isinstance(payload, Mapping) or not isinstance(payload.get("found"), bool):
            raise ObservationProtocolError("invalid observation owner response")
        if payload["found"] is False:
            if set(payload) - {"found", "reason"} or not isinstance(payload.get("reason"), str) or not payload["reason"]:
                raise ObservationProtocolError("invalid unavailable-observation response")
            raise ObservationUnavailable(payload["reason"])
        base_keys = {"found", "state", "observation"}
        if set(payload) not in (base_keys, base_keys | {"lineage"}):
            raise ObservationProtocolError("fresh observation and state are both required")
        lineage = payload.get("lineage")
        if lineage is not None:
            if not isinstance(lineage, Mapping) or set(lineage) != {"registrationId", "intentId"}:
                raise ObservationProtocolError("invalid observation lineage")
            if any(value is not None and (not isinstance(value, str) or not value or len(value) > 256)
                   for value in lineage.values()):
                raise ObservationProtocolError("invalid observation lineage identity")
            if (lineage["registrationId"] is None) != (lineage["intentId"] is None):
                raise ObservationProtocolError("incomplete observation lineage")
            lineage = dict(lineage)
        try:
            state = parse_conversation_state(payload["state"])
            observation = parse_observation(payload["observation"])
        except Exception as error:
            raise ObservationProtocolError(str(error)) from error
        raw_state, obs = state.to_dict(), observation.to_dict()
        if (obs["source"] != "browser"
                or _conversation_id(raw_state["target"]) != expected_id
                or _conversation_id(obs["target"]) != expected_id
                or raw_state["conversationId"] != obs["conversationId"]
                or any(raw_state["turn"][key] != obs[key]
                       for key in ("turnId", "userMessageId", "assistantMessageId"))):
            raise ObservationProtocolError("observation/state identity mismatch")
        if raw_state["writer"]["mode"] != "managed":
            raise ObservationProtocolError("writer_mode_mismatch")
        observed_at = datetime.fromisoformat(obs["observedAt"].replace("Z", "+00:00").replace("z", "+00:00"))
        age = self._clock() - observed_at.timestamp()
        if age < -5.0 or age > self.max_age_seconds:
            raise ObservationUnavailable("observation_stale")
        return ObservationSample(state, observation, lineage)


class SidecarObservationPage:
    """Per-tick adapter for the existing mechanical phase loop."""
    def __init__(self, target_url, observation_client, intent_client, *, sleep=time.sleep,
                 refresh_on_snapshot=False, registration_id=None):
        self.target_url = target_url
        self._observations = observation_client
        self._intents = intent_client
        self._sleep = sleep
        self._refresh_on_snapshot = refresh_on_snapshot
        self.registration_id = registration_id
        self._sample = None
        self._current_available = False
        self.diagnostics = {"observation_available": False}
        # Durable registry ownership is bound before the first observation.
        # This lets the owner adopt an existing desired target without a
        # constructor-time read creating a dependency on prior adoption.
        if not refresh_on_snapshot:
            self._sample = self._observations.read(target_url)
            self._current_available = True
            self._update_diagnostics()

    def _update_diagnostics(self):
        state, obs = self._sample.state.to_dict(), self._sample.observation.to_dict()
        self.diagnostics = {
            "observation_available": True, "observation_source": obs["source"],
            "observation_observed_at": obs["observedAt"],
            "state_version": state["stateVersion"], "writer_epoch": state["writer"]["epoch"],
            "observation_readable": obs["readable"], "observation_generating": obs["generating"],
            "observation_terminal": obs["terminal"], "observation_body": obs["body"],
            "observation_human_gate": obs["humanGate"],
        }
        snapshot = self._sample.snapshot()
        if snapshot.fault_text:
            self.diagnostics["reason"] = snapshot.fault_text

    def current_url(self):
        return self._sample.observation.to_dict()["target"] if self._sample is not None else self.target_url

    def snapshot(self):
        if self._refresh_on_snapshot:
            self._current_available = False
            self._sample = self._observations.read(self.target_url)
            self._current_available = True
            self._update_diagnostics()
        return self._sample.snapshot()

    @property
    def completion_text(self):
        return self._sample.snapshot().assistant_text if self._current_available else None

    def read(self, target):
        if not self._current_available:
            raise ObservationUnavailable("observation_unavailable")
        if _conversation_id(target) != _conversation_id(self.target_url):
            raise ObservationProtocolError("observation/state target mismatch")
        state = self._sample.state.to_dict()
        if state["gate"] == "human_required":
            return self._sample.state
        current = self._sample.snapshot()
        if current.fault_text or current.user_turn_pending:
            raise ObservationProtocolError(current.fault_text or "user_turn_pending")
        return self._sample.state

    def bind_registration(self, registration_id):
        self._intents.bind_watch(registration_id, self.target_url)
        self.registration_id = registration_id

    def submit_v1(self, state, text=None, *, source="watchdog", action="continue"):
        current = self._sample.snapshot() if self._current_available else None
        safe_phases = (Phase.THINKING, Phase.RESPONDING) if action == "stop" else (Phase.FINISHED, Phase.BLOCKED)
        if (current is None or current.interaction_required or current.user_turn_pending
                or current.phase not in safe_phases):
            return {"accepted": False, "reason": "observation_not_actionable"}
        return self._intents.submit_v1(state, text, source=source, action=action,
                                       registration_id=self.registration_id)

    def _submit(self, text=None, *, action="continue"):
        if not self.registration_id:
            self.diagnostics["intent_reason"] = "registration_unbound"
            return PromptDelivery(accepted=False)
        try:
            result = self.submit_v1(self._sample.state, text, action=action)
        except Exception as error:
            self.diagnostics.update(intent_reason="delivery_uncertain", intent_error=str(error)[:1000])
            return PromptDelivery(accepted=False, uncertain=True)
        reason = result.get("reason")
        self.diagnostics.update(intent_reason=reason, intent_accepted=result["accepted"])
        uncertain = result.get("uncertain") is True or reason in {
            "delivery_uncertain", "state_delivery_uncertain", "stop_outcome_unknown",
        }
        stale = reason in {"stale_state", "stale_intent", "writer_epoch_mismatch",
                           "target_unavailable", "user_turn_pending"}
        return PromptDelivery(
            accepted=result["accepted"] is True and not uncertain,
            message_id=result.get("messageId") or result.get("userMessageId") or "",
            stale=stale, uncertain=uncertain,
        )

    def proves_user_turn(self, user_turn_id):
        if not self._current_available or not user_turn_id:
            return False
        state, obs = self._sample.state.to_dict(), self._sample.observation.to_dict()
        # A pending human turn need not yet have an assistant or finality.
        # This is only persistent-user evidence, never permission to write.
        return bool(obs["readable"] is True and obs["userMessageId"] == user_turn_id
                    and obs["delivery"] == "delivered" and state["delivery"] == "delivered")

    def review_lineage_matches(self, expected_intent_id, registration_id):
        lineage = self._sample.lineage or {}
        return bool(expected_intent_id and registration_id
                    and lineage.get("intentId") == expected_intent_id
                    and lineage.get("registrationId") == registration_id)

    def prepare_simple_continue(self, prompt, expected_turn_key):
        current = self.snapshot()
        if current.turn_key != expected_turn_key or current.phase is not Phase.FINISHED:
            raise ObservationProtocolError("observation_not_actionable")
        return self._intents.prepare_v1(self._sample.state, prompt,
                                        registration_id=self.registration_id)["intentId"]

    def send_simple_continue(self, prompt, expected_turn_key, acceptance_timeout=0.0):
        snapshot = self.snapshot()
        if snapshot.turn_key != expected_turn_key or snapshot.phase is not Phase.FINISHED:
            return PromptDelivery(accepted=False, stale=True)
        return self._submit(prompt)

    def recover_stalled_active(self, prompt, expected_turn_key, *, stop_timeout=10.0,
                               acceptance_timeout=90.0, before_continue=None):
        before = self.snapshot()
        if before.turn_key != expected_turn_key or before.phase not in (Phase.THINKING, Phase.RESPONDING):
            return PromptDelivery(accepted=False, stale=True)
        stop = self._submit(action="stop")
        if not stop.accepted:
            return stop
        # An owner ACK only acknowledges its documented write attempt. Capture
        # actual stopped evidence before publishing the recovery continuation.
        deadline = time.monotonic() + stop_timeout
        while True:
            try:
                self._sample = self._observations.read(self.target_url)
            except (ObservationUnavailable, ObservationProtocolError) as error:
                self.diagnostics.update(recovery_reason="stopped_observation_unavailable",
                                        recovery_error=str(error)[:1000])
                return PromptDelivery(accepted=False)
            self._update_diagnostics()
            current = self.snapshot()
            if (current.user_turn_id != before.user_turn_id
                    or current.turn_key != before.turn_key
                    or current.assistant_text_signature != before.assistant_text_signature):
                self.diagnostics["recovery_reason"] = "conversation_progressed"
                return PromptDelivery(accepted=False, stale=True)
            obs = self._sample.observation.to_dict()
            if (obs["readable"] and obs["generating"] is False
                    and obs["terminal"] is not None and obs["humanGate"] is False
                    and not current.interaction_required and not current.user_turn_pending):
                if before_continue is not None:
                    try:
                        intent_id = self._intents.prepare_v1(
                            self._sample.state, prompt, registration_id=self.registration_id,
                        )["intentId"]
                        before_continue(intent_id)
                    except Exception as error:
                        self.diagnostics.update(recovery_reason="recovery_reservation_failed",
                                                recovery_error=str(error)[:1000])
                        return PromptDelivery(accepted=False)
                return self._submit(prompt)
            if time.monotonic() >= deadline:
                self.diagnostics["recovery_reason"] = "stop_not_observed"
                return PromptDelivery(accepted=False)
            self._sleep(min(0.25, max(0.0, deadline - time.monotonic())))

    def refresh(self):
        if not self.registration_id:
            self.diagnostics["recovery_reason"] = "registration_unbound"
            return PromptDelivery(accepted=False)
        current = self.snapshot()
        if current.phase is not Phase.BLOCKED or current.interaction_required or current.user_turn_pending:
            return PromptDelivery(accepted=False, stale=True)
        try:
            result = self._intents.refresh_v1(self._sample.state, registration_id=self.registration_id)
        except Exception as error:
            self.diagnostics.update(recovery_reason="delivery_uncertain", recovery_error=str(error)[:1000])
            return PromptDelivery(accepted=False, uncertain=True)
        reason = result.get("reason")
        self.diagnostics.update(recovery_reason=reason, recovery_accepted=result["accepted"])
        return PromptDelivery(accepted=result["accepted"],
                              uncertain=reason == "delivery_uncertain",
                              stale=reason in {"stale_state", "stale_intent", "writer_epoch_mismatch"})

    def close(self):
        return None
