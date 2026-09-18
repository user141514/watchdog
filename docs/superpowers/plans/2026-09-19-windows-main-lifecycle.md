# Windows main and durable lifecycle

## Outcome and constraints

One shared `main`, verified on Windows, instead of one branch per version. Preserve all existing platform behavior and the Sidecar single-writer contract. Work in `E:/DevSpace/worktrees/watchdog-73ebe8a5`; do not alter active browser conversations, shared DevSpace, Orca, or other worktrees. Use an isolated Python venv, not Conda. Never register a new target without requester/explicit-target authority. No production fault injection or blind retries after uncertain delivery.

## Evidence and minimal model

2026-09-19: origin/main=20c17a8; remote development tip=9853a0a. Local main=621c30a adds blocked-turn recovery already superseded by guarded recovery in the development line. All other local Windows version refs are ancestors of 9853a0a. Source and installed runtime registry.py/supervisor.py have identical git blob hashes. The active registry on port 9235 is the installed package under E:/DevRuntime/chat-watchdog-venv, not the canonical checkout. An additional legacy compatibility process uses E:/Dev/chat-watchdog. PC1 connector is unavailable; its previously reported unpushed authority commits cannot be retrieved from GitHub and must not be claimed integrated.

Model: durable desired registration -> per-conversation watcher -> authoritative Sidecar state -> versioned intent -> Sidecar single browser writer. Desired registration is not connectivity, connectivity is not progress, and accepted intent is not proven delivery. Current WatchRegistry retains active watches and completion receipts only in memory; step_all lets one uncaught watcher exception abort the complete loop. HTTP control and polling share one thread. Reload changes transport instances, not conversation identity or user authorization.

## Invariants and counterexamples

- A successfully acknowledged registration survives process termination. Restart while the browser/relay is unavailable retains desired watches and retries binding; it must not require user re-registration.
- One failed watcher cannot stop polling other watches. Errors are visible with timestamps and retry state, not swallowed or converted to completion.
- Unregister wins over stale polling snapshots; after unregister returns, that generation cannot write. Completion is atomic with removing active registration, and its receipt survives restart until ACK.
- Sidecar remains the only managed writer. UNKNOWN, human-required, uncertain delivery, stale epoch/version, pending user turn and unavailable targets never trigger a direct-send fallback.
- Registry health proves recent polling, not just an open port. A stalled dependency must not block the control endpoint.
- No unmerged branch or dirty worktree is deleted. Main is fast-forwarded only after overlap review, tests and build; deleted branch tips retain backup tags.

## Implementation and verification sequence

- [x] Fetch origin, compare ancestry, record runtime authority, create isolated worktree and backup tags.
- [ ] Merge local main history into current development line; retain newer guarded recovery, run inherited regressions.
- [ ] Add failing restart/unavailable-startup/failure-isolation/unregister-race/completion-receipt tests. Add transactional SQLite storage at the registry boundary, lazy reconnection and per-entry serialization. Keep library in-memory construction supported for unit tests; production CLI must select durable storage explicitly/default safely.
- [ ] Add health/control responsiveness and deployment identity checks. Exercise HTTP and real subprocess crash/restart against disposable local fixtures, never production conversations.
- [ ] Review authoritative continuation acceptance against downstream delivery and fault-recovery semantics; add counterexamples before modifying policy.
- [ ] Run all Python tests, compileall, targeted quality checks, package build, installed-wheel tests and Windows isolated live gate. Obtain an independent read-only adversarial review when an available worker route is verified; do not repair unrelated orchestration infrastructure.
- [ ] Publish and fast-forward main; verify remote exact SHA and zero behind. Delete only integrated Windows version refs after backup, leaving dirty/active worktrees intact.
- [ ] Deploy only after preserving active target/state evidence and a reversible handover. Re-read actual runtime after reload/restart; distinguish source/published/installed/live evidence. Report any gate not closed rather than calling it complete.

## Rollback

Tags `backup/windows-main-20260919` (621c30a) and `backup/windows-runtime-20260919` (9853a0a) preserve starting tips. Existing checkout, live installed wheel and legacy data are untouched during implementation. New durable store must not open/overwrite the incompatible legacy registry database. Any runtime handover needs its own exact old command/package/state receipt before stopping only the attributable watchdog process.
