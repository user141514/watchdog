# Dynamic Watch Registry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the fixed single-conversation watchdog deployment model with a durable dynamic registry that can supervise multiple exact ChatGPT conversations without restarting the daemon.

**Architecture:** Add a standard-library SQLite registry above the existing `RelayChatGPTPage` + `Supervisor` pair. A daemon reconciles active registry entries into independent existing Supervisor runtimes; the original single-conversation CLI remains unchanged.

**Tech Stack:** Python 3, stdlib `sqlite3`, `argparse`, existing Watchdog Supervisor/relay/reanchor modules, pytest.

**Spec:** `docs/superpowers/specs/2026-09-13-watch-registry-design.md`

## Global Constraints

- Do not change existing `Supervisor` recovery/finality semantics.
- Do not infer targets from browser focus or partial project URLs.
- Registry targets are exact canonical `https://chatgpt.com/.../c/<id>` URLs.
- DONE must persist as completed and must not restart after daemon restart.
- Existing `chat-watchdog --match-url` behavior remains compatible.
- Use only Python standard-library persistence; no new runtime dependency.

---

### Task 1: Persistent Watch Registry

**Files:**
- Create: `chat_watchdog/registry.py`
- Create: `tests/test_registry.py`

**Interfaces:**
- Produces: `WatchEntry`, `WatchRegistry`, `canonical_conversation_url()`.
- `WatchRegistry.add(watch_id, target_url, reanchor_scope=None, reanchor_epoch=None) -> WatchEntry`
- `WatchRegistry.list(state=None) -> list[WatchEntry]`
- `WatchRegistry.get(watch_id) -> WatchEntry | None`
- `WatchRegistry.set_state(watch_id, state) -> WatchEntry`
- `WatchRegistry.record_result(watch_id, result) -> WatchEntry`
- `WatchRegistry.remove(watch_id) -> bool`

- [ ] **Step 1: Write failing registry tests**

Cover canonical URL validation, durable reopen, duplicate URL rejection, reanchor scope/epoch pairing, state transitions, result persistence, and removal.

- [ ] **Step 2: Run registry tests and verify RED**

Run: `python -m pytest tests/test_registry.py -q`
Expected: import/module failure because `chat_watchdog.registry` does not exist.

- [ ] **Step 3: Implement minimal SQLite registry**

Use one `watches` table, SQLite transactions, UTC ISO timestamps, and exact enum validation for `active|paused|completed`.

- [ ] **Step 4: Run registry tests and verify GREEN**

Run: `python -m pytest tests/test_registry.py -q`
Expected: all registry tests pass.

- [ ] **Step 5: Commit**

Commit message: `feat: add durable watchdog registry`

### Task 2: Dynamic Daemon Reconciliation

**Files:**
- Create: `chat_watchdog/registry_daemon.py`
- Create: `tests/test_registry_daemon.py`

**Interfaces:**
- Consumes: `WatchRegistry`, existing `RelayChatGPTPage`, `Supervisor`, `AgentPool`.
- Produces: `WatchDaemon.reconcile()`, `WatchDaemon.step()`, `WatchDaemon.close()`.
- Runtime factory interface: callable `(WatchEntry) -> object` whose runtime exposes `supervisor` and `close()`; production factory builds the existing page/Supervisor pair. This injection keeps unit tests browser-free.

- [ ] **Step 1: Write failing daemon tests**

Test dynamic add after daemon startup, pause/remove closes only that runtime, DONE persists completion, completed entries are not recreated, and one runtime failure does not block a sibling.

- [ ] **Step 2: Run daemon tests and verify RED**

Run: `python -m pytest tests/test_registry_daemon.py -q`
Expected: import/module failure.

- [ ] **Step 3: Implement reconciliation and runtime isolation**

Keep one runtime map keyed by `watch_id`; reconcile against active registry rows each cycle. Persist `last_result`; mark completed on DONE/`should_stop`; catch per-watch exceptions and continue siblings.

- [ ] **Step 4: Run daemon tests and verify GREEN**

Run: `python -m pytest tests/test_registry_daemon.py -q`
Expected: all daemon tests pass.

- [ ] **Step 5: Commit**

Commit message: `feat: supervise dynamic watchdog registry`

### Task 3: Registry CLI and Backward Compatibility

**Files:**
- Create: `chat_watchdog/registry_cli.py`
- Create: `tests/test_registry_cli.py`
- Modify: `pyproject.toml`
- Modify: `README.md`

**Interfaces:**
- Produces console command `chat-watchdog-registry` with `add`, `list`, `pause`, `arm`, `remove`, `run`.
- `run` accepts the existing relay/agent/recovery settings and optional daemon-level reanchor installation settings; entry-level scope/epoch come from the registry.

- [ ] **Step 1: Write failing CLI tests**

Verify CRUD JSON output and that `run` parser accepts registry/relay/agent settings without changing the existing `chat-watchdog` parser.

- [ ] **Step 2: Run CLI tests and verify RED**

Run: `python -m pytest tests/test_registry_cli.py -q`
Expected: import/module failure.

- [ ] **Step 3: Implement CLI and console entry point**

Use argparse subcommands. Keep human list output compact and `--json` machine-readable.

- [ ] **Step 4: Run focused and existing regressions**

Run: `python -m pytest -q`
Expected: registry tests plus original 11 tests all pass.

- [ ] **Step 5: Commit**

Commit message: `feat: expose watchdog registry cli`

### Task 4: devnbook9 Deployment and Live Gate

**Files:**
- No source changes unless live evidence reveals a defect.
- Runtime state: user-scoped registry DB and one daemon process.

**Interfaces:**
- Current exact ChatGPT conversation URL is discovered from the existing relay `/json/list`, then registered explicitly.
- Existing old fixed-URL watchdog remains running until the registry daemon proves healthy; only then is it retired.

- [ ] **Step 1: Run full Python regression suite**

Run: `python -m pytest -q`
Expected: zero failures.

- [ ] **Step 2: Install/update the Python package without changing reanchor**

Use the existing Python environment that currently owns `chat-watchdog.exe`; do not start Node CLI processes.

- [ ] **Step 3: Register current exact `agent` conversation**

Read relay `/json/list`, select the exact current `ChatGPT - agent` URL, and add it under a stable watch ID.

- [ ] **Step 4: Start one registry daemon and verify dynamic state**

Confirm the daemon sees the registered entry and attaches to the exact target. Add or pause a disposable second registry entry and verify reconciliation without restarting the daemon.

- [ ] **Step 5: Retire old fixed watcher only after the new daemon is proven**

Verify no remaining `chat-watchdog --match-url /c/6aa525c0...` process remains and exactly one registry daemon owns the new deployment.

- [ ] **Step 6: Final evidence**

Report registry rows, daemon process identity, exact monitored conversation URL, and Python test result. Do not claim reanchor coverage unless a registry entry was explicitly configured with a valid scope/epoch and exercised.
