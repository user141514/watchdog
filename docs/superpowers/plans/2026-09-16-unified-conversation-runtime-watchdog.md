# Unified Conversation Runtime — Watchdog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert managed Watchdog from a local lifecycle owner into a policy/intent producer that consumes Sidecar authoritative conversation state.

**Architecture:** Add pure contract types first, then add a Sidecar state reader, then migrate Supervisor policy to consume authoritative state and publish versioned intents. Relay remains an observation source during transition, never a writer or final state authority.

**Tech Stack:** Python 3.12, existing pytest suite, localhost Sidecar HTTP endpoints, existing Relay observation adapter.

**Spec:** `../specs/2026-09-16-unified-conversation-runtime-watchdog.md`; canonical schema authority is the Sidecar spec named there.

## Global Constraints

- Only devnbook9; existing worktree `E:/DevSpace/worktrees/watchdog-5a0ae028`.
- Preserve explicit `--legacy-direct-send` separation; managed mode cannot regain Relay mutation authority.
- No new broker/daemon/dependency.
- Every task uses RED -> minimal GREEN -> focused regression -> software-engineering audit -> commit.
- Cross-project slices are accepted only after Sidecar counterpart contract tests pass.

---

### Task 1: Python v1 contract types

**Files:**
- Create: `chat_watchdog/contracts.py`
- Create: `tests/test_contracts.py`

**Interfaces:**
- Produces immutable/value-like `ConversationStateV1` and `IntentEnvelopeV1` parser/builders.
- Consumes canonical field/enumeration semantics from Sidecar spec; does not define independent alternatives.

- [ ] RED tests: valid v1 state/intent; unknown version rejected; extra fields rejected; existing-conversation mutation requires `expectedStateVersion`; REUSE requires exact target; unknown state remains unknown.
- [ ] Run `.venv/Scripts/python.exe -m pytest -q tests/test_contracts.py`; expect import failure.
- [ ] Implement strict parsers/builders with no HTTP, Relay, clock, or Supervisor imports.
- [ ] Run focused tests.
- [ ] Cross-project gate: compare semantic fixtures with Sidecar `test/fixtures/conversation-runtime-v1.json` and Node parser results.
- [ ] Software-engineering audit: malformed/future/stale payload counterexamples; verify no behavior call sites changed.
- [ ] Commit `feat: define conversation runtime v1 watchdog contract`.

### Task 2: Sidecar authoritative state client

**Files:**
- Create: `chat_watchdog/state_client.py`
- Modify: `chat_watchdog/cli.py`
- Test: `tests/test_state_client.py`, CLI tests.

**Interfaces:**
- Consumes Sidecar authoritative `ConversationStateV1` for exact target.
- Produces typed state or explicit unavailable/invalid error; never invents state from HTTP failure.

- [ ] RED: localhost-only endpoint validation; malformed response fails closed; unknown version fails closed; target identity mismatch fails closed.
- [ ] Implement client with bounded timeout and no fallback to Relay mutation.
- [ ] Managed CLI constructs both state and intent clients from the same Sidecar owner base URL.
- [ ] Audit outage behavior: Sidecar unavailable => watch waits/unknown; no legacy fallback.
- [ ] Commit `feat: read authoritative conversation state from sidecar`.

### Task 3: Policy consumes authoritative state

**Files:**
- Modify: `chat_watchdog/supervisor.py`
- Modify: `chat_watchdog/chatgpt_page.py` only to keep Relay facts as observation evidence if needed.
- Test: `tests/test_supervisor_intents.py`, new state-policy tests.

**Interfaces:**
- Consumes `ConversationStateV1`.
- Produces `IntentEnvelopeV1` with exact `expectedStateVersion`, message identities, writer epoch, stable intent identity.

- [ ] RED: `blocked+incomplete+no gate` emits one continuation intent.
- [ ] RED: `unknown`, `human_required`, `delivery_uncertain`, `active` do not emit continuation.
- [ ] RED: stale-state receipt causes re-observation, never direct retry.
- [ ] RED: local Relay `BLOCKED` conflicting with Sidecar `active` follows Sidecar state.
- [ ] Implement policy adapter; retain local snapshot only as observation/debug data.
- [ ] Audit liveness isolation across multiple watches.
- [ ] Commit `refactor: drive watchdog policy from sidecar state`.

### Task 4: Versioned intent publishing and compatibility

**Files:**
- Modify: `chat_watchdog/intent_client.py`
- Modify: `chat_watchdog/supervisor.py`
- Test: `tests/test_intent_client.py`, integration tests.

**Interfaces:**
- Publishes canonical `IntentEnvelopeV1`.
- Temporarily accepts Sidecar compatibility response while Sidecar adapter exists.

- [ ] RED: exact stateVersion/epoch/message IDs cross unchanged.
- [ ] RED: `stale_state` records handled/waiting semantics without duplicate browser effect.
- [ ] RED: unsupported contract response fails closed.
- [ ] Implement versioned payload path; do not add direct send fallback.
- [ ] Audit duplicate intent identity and restart behavior.
- [ ] Commit `feat: publish versioned watchdog intents`.

### Task 5: Remove competing state authority and integrate

**Files:**
- Narrow/remove managed-mode local lifecycle authority only after Sidecar reducer/dispatcher is deployed.
- Update README/docs.

- [ ] Full Watchdog pytest + pip check.
- [ ] Full Sidecar suite against current Watchdog branch.
- [ ] Real controlled watches: active, blocked-incomplete continuation, terminal, human gate, state-version race.
- [ ] Confirm daemon command line has intent/state owner endpoints and no `--legacy-direct-send`.
- [ ] Broad adversarial review of both branch diffs.
- [ ] Install Watchdog package only after Sidecar runtime compatibility gate is green.
