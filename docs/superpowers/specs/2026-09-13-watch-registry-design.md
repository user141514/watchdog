# Dynamic Watch Registry Design

## Problem

The current deployment launches one `chat-watchdog` process with one fixed `--match-url`. That process can only observe the conversation selected at startup. A long-running task in any other ChatGPT conversation is outside the observation set, so no watchdog action can occur even when the Supervisor logic is correct.

The observed failure on devnbook9 is exactly this case: the running watchdog was bound to `/c/6aa525c0-e748-83ea-a617-df96d68d63ec`, while the active `agent` task was in a different conversation. The process was healthy but semantically attached to the wrong target.

## Goal

Make the monitored conversation set explicit, durable, dynamic, and machine-readable without changing the existing per-conversation recovery policy.

## Non-goals

- Do not rewrite `Supervisor` or its finality/recovery logic.
- Do not infer the target from whichever browser tab is focused.
- Do not auto-retarget a reanchor scope; current reanchor target identity remains immutable.
- Do not require Node or a new external service.
- Do not turn the registry into a general task scheduler.

## Architecture

Introduce a persistent SQLite-backed Watch Registry above the existing single-conversation Supervisor.

```text
Sidecar / Sidebar / operator
        |
        | add/pause/arm/remove exact conversation URL
        v
WatchRegistry (SQLite)
        |
        | active entries
        v
WatchDaemon
  watch-a -> RelayChatGPTPage -> Supervisor
  watch-b -> RelayChatGPTPage -> Supervisor
  watch-c -> RelayChatGPTPage -> Supervisor
```

Each active registry entry owns one page attachment and one Supervisor. The daemon polls the registry as well as the watched conversations. Adding an entry starts supervision without restarting the daemon. Removing or pausing an entry closes only that runtime. A Supervisor that reaches DONE marks its registry entry `completed`, preventing automatic recreation on the next daemon pass. `arm` explicitly returns a paused/completed entry to `active`.

## Persistent model

SQLite is used because it is in Python's standard library, works on Windows/Linux, and provides transactional concurrent updates without an ad-hoc lock-file protocol.

Table `watches`:

- `watch_id TEXT PRIMARY KEY`: stable explicit identifier, 1-64 `[A-Za-z0-9_.-]` characters.
- `target_url TEXT NOT NULL UNIQUE`: exact canonical ChatGPT conversation URL.
- `state TEXT NOT NULL`: `active`, `paused`, or `completed`.
- `reanchor_scope TEXT NULL`: optional already-initialized reanchor scope.
- `reanchor_epoch TEXT NULL`: required whenever `reanchor_scope` is present.
- `last_result TEXT NULL`: most recent Supervisor `StepResult` value.
- `created_at TEXT NOT NULL`, `updated_at TEXT NOT NULL`: UTC ISO timestamps.

Reanchor store/CLI/owner/context-root remain daemon-level configuration because they describe the local reanchor installation; scope/epoch remain entry-level because they bind one logical task/conversation.

## Identity rules

A registry target must be a canonical ChatGPT conversation URL:

- origin `https://chatgpt.com`
- path contains a terminal `/c/<conversation-id>` segment
- fragments and query strings are rejected

The full canonical URL is passed as `match_url`. The existing relay selector therefore attaches only to the intended page rather than a partial project or conversation substring.

Duplicate target URLs are rejected. A watch ID never silently changes target; callers must remove the old entry and add a new one. This preserves auditability and avoids implicit retargeting.

## Daemon reconciliation

On every daemon cycle:

1. Load active registry entries.
2. Close runtimes whose watch was removed, paused, completed, or whose immutable configuration changed externally.
3. Create runtimes for active entries that have no runtime.
4. Step each runtime once.
5. Persist `last_result` after every successful step.
6. If `Supervisor.should_stop` or result is `DONE`, mark the entry `completed` and close that runtime.
7. A per-watch connection failure is logged and retried on a later cycle; it must not terminate the daemon or affect sibling watches.

The daemon does not resend merely because a registry/relay operation failed. Send/finality semantics remain inside the existing Supervisor and RelayChatGPTPage contracts.

## CLI

Keep the existing `chat-watchdog --match-url ...` command unchanged for backward compatibility.

Add `chat-watchdog-registry`:

- `add WATCH_ID TARGET_URL [--reanchor-scope SCOPE --reanchor-epoch EPOCH]`
- `list [--json]`
- `pause WATCH_ID`
- `arm WATCH_ID`
- `remove WATCH_ID`
- `run [existing agent/relay/recovery options]`

Default store: `~/.chat-watchdog/watch-registry.sqlite3`. Override with `--store` or `CHAT_WATCHDOG_REGISTRY`.

## Safety invariants

- Exact target identity is explicit and durable.
- One failing watch cannot stop sibling watches.
- DONE is durable; daemon restart cannot accidentally resume a completed watch.
- Reanchor is enabled only when both scope and epoch are configured for an entry and daemon-level reanchor installation arguments are complete.
- No implicit browser-focus discovery.
- No implicit retargeting.
- Existing one-conversation CLI remains behaviorally unchanged.

## Acceptance

1. Existing 11 Python regression tests still pass.
2. Registry CRUD is transactional and survives reopen.
3. Duplicate URL / invalid URL / invalid reanchor pair is rejected.
4. Daemon starts a runtime when an active entry appears after daemon startup.
5. Daemon closes only the removed/paused runtime.
6. DONE persists as `completed` and is not recreated after daemon reconciliation or process restart.
7. One runtime connection/step failure does not prevent a sibling from being stepped.
8. On devnbook9, replace the fixed single-URL watcher deployment with one registry daemon, register the current exact `agent` conversation, and verify its runtime state from the registry/relay without restarting the daemon.
