# Windows main and durable lifecycle

## Outcome and constraints

One shared `main`, verified on Windows, instead of one branch per version. Preserve all existing platform behavior and the Sidecar single-writer contract. Work in `E:/DevSpace/worktrees/watchdog-73ebe8a5`; do not alter active browser conversations, shared DevSpace, Orca, or other worktrees. Use an isolated Python venv, not Conda. Never register a new target without requester/explicit-target authority. No production fault injection or blind retries after uncertain delivery.

## Evidence and minimal model

2026-09-19: origin/main=20c17a8; remote development tip=9853a0a. Local main=621c30a adds blocked-turn recovery already superseded by guarded recovery in the development line. All other local Windows version refs are ancestors of 9853a0a. Source and installed runtime registry.py/supervisor.py have identical git blob hashes. The active registry on port 9235 is the installed package under E:/DevRuntime/chat-watchdog-venv, not the canonical checkout. An additional legacy compatibility process uses E:/Dev/chat-watchdog. PC1 connector is unavailable; its previously reported unpushed authority commits cannot be retrieved from GitHub and must not be claimed integrated.

Model: durable desired registration -> per-conversation watcher -> authoritative Sidecar state -> versioned intent -> Sidecar single browser writer. Desired registration is not connectivity, connectivity is not progress, and accepted intent is not proven delivery. At the inspected baseline, WatchRegistry retained active watches and completion receipts only in memory; step_all lets one uncaught watcher exception abort the complete loop. HTTP control and polling share one thread. Reload changes transport instances, not conversation identity or user authorization.

## Invariants and counterexamples

- A successfully acknowledged registration survives process termination. Restart while the browser/relay is unavailable retains desired watches and retries binding; it must not require user re-registration.
- One failed watcher cannot stop polling other watches. Errors are visible with timestamps and retry state, not swallowed or converted to completion.
- Unregister wins over stale polling snapshots; after unregister returns, that generation cannot write. Completion is atomic with removing active registration, and its receipt survives restart until ACK.
- Sidecar remains the only managed writer. UNKNOWN, human-required, uncertain delivery, stale epoch/version, pending user turn and unavailable targets never trigger a direct-send fallback.
- Registry health proves recent polling, not just an open port. A stalled dependency must not block the control endpoint.
- No unmerged branch or dirty worktree is deleted. Main is fast-forwarded only after overlap review, tests and build; deleted branch tips retain backup tags.

## Implementation and verification sequence

- [x] Fetch origin, compare ancestry, record runtime authority, create isolated worktree and backup tags.
- [x] Merge local main history into current development line; retain newer guarded recovery, run inherited regressions.
- [x] Add failing restart/unavailable-startup/failure-isolation/unregister-race/completion-receipt tests. Add transactional SQLite storage at the registry boundary, lazy reconnection and per-entry serialization. Keep library in-memory construction supported for unit tests; production CLI must select durable storage explicitly/default safely.
- [x] Add health/control responsiveness and deployment identity checks. Exercise HTTP and real subprocess crash/restart against disposable local fixtures, never production conversations.
- [x] Review authoritative continuation acceptance against downstream delivery and fault-recovery semantics; add counterexamples before modifying policy.
- [x] Run all Python tests, compileall, targeted quality checks, package build, installed-wheel tests and Windows isolated process gate: 141 tests plus 14 subtests passed both source and installed wheel; 5 Node tests and syntax checks passed. Four GitHub CI jobs passed on Windows/Ubuntu with Python 3.11/3.12.
- [x] Add and verify CDP total-deadline, socket-cleanup and fail-closed durable legacy-replay counterexamples.
- [ ] Independent read-only adversarial review: worker routes did not yield an available reviewer. Do not claim this gate passed; unrelated orchestration infrastructure was not modified.
- [x] Publish and fast-forward main; verify remote exact SHA and zero behind. Delete only integrated Windows version refs after backup, leaving dirty/active worktrees intact.
- [ ] Production handover: stopped before any production mutation because the fresh 9235 inventory grew from two to three active registrations, proving concurrent usage. The legacy compatibility service has another three distinct active registrations. The tested release is staged; neither live process, its registrations, its scheduled task, nor the browser extension was changed.

## Verified closeout

Runtime code is published at `6807f54` / tag `v0.2.0`. Three integrated remote branches and six local Windows version branches were retired after archive tags were published; main was reread at zero ahead/behind. Occupied worktree branch labels remain local without losing their changes. See `docs/releases/2026-09-19-v0.2.0-verification.md` for exact artifact/CI evidence, boundaries and the remaining production handover gate.

## Rollback

Tags `backup/windows-main-20260919` (621c30a) and `backup/windows-runtime-20260919` (9853a0a) preserve starting tips. The canonical checkout was fast-forwarded only after source/install verification. Live installed packages and legacy data remain untouched. New durable store must not open/overwrite the incompatible legacy registry database. Any runtime handover needs its own exact old command/package/state receipt before stopping only the attributable watchdog process.
