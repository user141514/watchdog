# Watchdog Obligations for Unified Conversation Runtime

## Canonical authority

The canonical `conversation-runtime/v1` field definitions and state semantics live in the Sidecar repository:

`docs/superpowers/specs/2026-09-16-unified-conversation-runtime-design.md`

This document does not redefine that schema. It records only Watchdog-owned obligations so the two repositories do not become independent protocol authorities.

## Outcome

Managed Watchdog becomes a policy node:

`authoritative ConversationState -> Watchdog policy -> IntentEnvelope`

Relay/browser snapshots remain observation inputs during migration, not a competing authoritative lifecycle.

## Invariants

1. Managed Watchdog never owns browser-write authority.
2. Managed Watchdog never upgrades a Relay-local phase into authoritative conversation completion.
3. A state-changing intent against an existing conversation carries the Sidecar `stateVersion` and exact message identities it was derived from.
4. `stale_state`, `delivery_uncertain`, `human_required`, and `unknown` are fail-closed; Watchdog never converts them into direct Relay mutation.
5. Local dedupe may suppress duplicate policy output, but cannot replace Sidecar request/effect identity.
6. One blocked/unknown watch cannot become global daemon liveness or writer authority for unrelated watches.
7. Legacy direct mode remains explicitly separate until removed by a later migration and cannot claim managed correctness.

## Migration

1. Add pure v1 contract types/parsers and fixtures with no behavior change.
2. Add a Sidecar state client and consume authoritative state in managed policy.
3. Publish versioned intents; retain temporary compatibility with the current intent endpoint only while Sidecar adapter exists.
4. Delete local authoritative lifecycle decisions only after cross-project acceptance proves equivalent behavior.

## Acceptance

- Python contract fixtures agree semantically with Sidecar v1 fixtures.
- Unknown contract versions and extra fields fail closed.
- State N policy intent is rejected after Sidecar reaches N+1; Watchdog observes the new state instead of retrying the stale intent.
- `progress=blocked/body=incomplete` may produce a continuation intent if no human gate exists.
- `progress=unknown`, `gate=human_required`, or `delivery=uncertain` never produces a browser mutation path.
- Managed mode cannot call `send_continue`, `retry_fault`, reanchor send, or recovery agent browser mutation.
