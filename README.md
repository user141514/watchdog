# chat-watchdog + reanchor

A bounded watchdog for explicitly authorized ChatGPT conversations. `main` is the shared development and release line, validated on Windows; versions belong in tags and package metadata, not accumulating Windows version branches. This project does not modify official DevSpace. The Python watchdog and the colocated Node reanchor package preserve the existing PC deployment layout.

Version 0.2.0 separates durable desired registration, current transport binding, recent polling, and the authoritative reason a conversation may or may not continue. A live process or registration ACK alone is not proof that work is advancing.

## Scope and invariants

The legacy `chat-watchdog` command observes one explicitly selected, already-open ChatGPT conversation through an independently owned OMP Browser Relay. Registry mode (`chat-watchdog --registry-port 9235`) persists explicitly requested watches in SQLite and maintains disposable bindings from ChatGPT's own conversation UUID to a per-conversation Supervisor, so exact conversations can be added or removed without restarting the daemon. Reanchor retains directives, observations, context identity, delivery identity and nonce-bound model checkpoints. A model checkpoint is advisory; it is not independent proof of semantic completion.

- Do not select a target by a partial or ambiguous match when multiple tabs qualify.
- Preserve the exact scope, context epoch, packet nonce and delivered message on recovery. Do not resend merely because a tool response or process was lost.
- A send ACK is not completion. `disabled=false` does not override `aria-disabled=true`.
- Absence of a stop button does not prove finality. Ambiguous or stale page state must remain blocked.
- A source installation, regression pass, runtime process and real browser acceptance are separate claims.
- Installing the package does not start a watcher or install a boot-time service, browser refresh policy or relay. Host services require an explicit, separately verified deployment.
- Keep live conversation fixtures, credentials, event stores, browser profiles and raw execution logs outside source control.

## Provenance

The imported baseline is the already-verified PC source under `C:/Users/Administrator/gitproject/chat-watchdog`. Its donor directories were `E:/Dev/chat-watchdog` and `E:/Dev/reanchor` on LAPTOP-M7FG2GG1; neither donor directory had Git history. This repository therefore begins with a source snapshot, not fabricated upstream history.

The PC source includes the previously accepted submit/finality corrections and the original nonce-validation contract. On 2026-09-12 the retained acceptance store was re-read as COMPLETE with no pending packet; the successful process was re-read as terminal, succeeded, exit 0 and quiescent. That controlled acceptance was not rerun for this import.

The original PC import contained 11 Python tests and 5 Node protocol tests, not the donor's complete historical suite. The Python suite has since expanded; run the commands below for current acceptance. Host-specific receipts remain in the local `DEPLOYMENT_STATUS.md` and in the separately versioned mymem project records.

## Install and verify

Use Python 3.11 or newer and an existing Node installation; the verified PC runs Node 24. Keep the installation in a stable checkout, not a disposable worktree.

Windows PowerShell:

```powershell
py -3 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -e '.[dev]'
& .\.venv\Scripts\python.exe -m pytest -q
npm --prefix reanchor test
npm --prefix reanchor run check
& .\.venv\Scripts\python.exe -m pip check
& .\.venv\Scripts\python.exe -m chat_watchdog --help
```

POSIX shell (installation recipe, not a claim of a new Linux live acceptance):

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
npm --prefix reanchor test
npm --prefix reanchor run check
.venv/bin/python -m pip check
.venv/bin/python -m chat_watchdog --help
```

The OMP relay, existing browser-extension trust and selected browser conversation must be verified independently before launching a watch. Consult `python -m chat_watchdog --help`, `chat-watchdog-registry --help`, and `node reanchor/bin/reanchor.mjs --help` for the installed command contracts. Do not copy a different host's live scope or conversation IDs into a new deployment.

## Dynamic watch registry

Registry mode owns a durable desired-watch table keyed by ChatGPT's own conversation UUID from `/c/<uuid>`. A normal URL such as `https://chatgpt.com/c/<uuid>` and a Project URL such as `https://chatgpt.com/g/<project>/c/<uuid>` therefore refer to the same watch. Browser title, focus and transient `PAGE...` target IDs are never identity.

```powershell
chat-watchdog --registry-port 9235 --relay-url http://127.0.0.1:9224 --poll-seconds 15 `
  --intent-url http://127.0.0.1:7337/internal/conversation-intents
# Replace only with a conversation explicitly authorized by the user.
chat-watchdog-registry add "https://chatgpt.com/c/<conversation-uuid>"
chat-watchdog-registry list --json
Invoke-RestMethod http://127.0.0.1:9235/health
chat-watchdog-registry remove "<conversation-uuid>"
```

The localhost control API exposes `POST /register`, `POST /unregister`, `GET /watches`, `GET /health`, `POST /completion`, and `POST /completion/ack`. Registration is idempotent by conversation UUID. The production daemon acknowledges the durable commit without waiting for a browser connection: the ACK means desired, not connected. On `SUPERVISOR_DONE`, active registration atomically becomes a completion receipt, retained across restart until ACK.

`GET /watches` preserves identity/state fields and adds polling timestamps, binding availability, consecutive failures, latest transport error and managed-state diagnostics. `connected` means a watcher binding exists, not current browser readiness. `last_success_at` means a watchdog step returned, not task progress. Inspect `diagnostics.state_available`, `progress`, `delivery`, `gate`, `writer_epoch` and `reason` too. Unknown state, uncertain delivery and human-required gates never authorize a direct-send fallback.

`GET /health` exposes process/module and store identities, completed polling time and fatal polling errors. `ready` means a fresh registry loop, not a continuable target. Slow HTTP clients cannot block other control requests. Watcher failures are retained for retry without aborting siblings; unregister fences stale poll snapshots and waits for its in-flight generation.

Managed mode now publishes intents to Sidecar, defaulting to `http://127.0.0.1:7337/internal/conversation-intents`. Use `--intent-url` to select another localhost Sidecar instance. The previous `--send-admission-url .../internal/send-admission` flag is accepted as a migration alias and translated to the sibling intent endpoint; it no longer grants direct browser-write authority.

The Supervisor receives an observation-only page adapter. Every continuation carries both expected user and assistant message IDs. Sidecar owns serialization, durable reservation, idempotency, current lifecycle checks and pacing. Missing owner, denial, lost receipt or invalid response never falls back to Relay writes or recovery agents. A receipt means submission, not completion. Frontend retry intents currently return `recovery_requires_reconciliation`; managed mode does not click Retry or invent another prompt after ambiguous delivery.

Unbrokered operation requires explicit `--legacy-direct-send`, cannot be combined with managed endpoint flags, and makes no cross-sender single-writer or pacing guarantee. Direct-write reanchor is available only in that explicitly selected legacy mode. Keep this boundary visible: the mailbox does not control a human clicking Send in another browser/profile.

Desired registration is durable until explicit unregister or verified completion. Closing or restarting the daemon is not unregister. Restore is lazy: an absent browser or reloading relay remains `reconnecting`; the watchdog never opens a new tab or chooses a replacement target. Sidecar still owns task state, writer authority and delivery deduplication. The database is not a second task planner and storing a URL does not grant new authority.

The Windows default is `%LOCALAPPDATA%/chat-watchdog/registry-v2.sqlite3`; POSIX uses `$XDG_STATE_HOME/chat-watchdog/registry-v2.sqlite3` (falling back to `~/.local/state`). Pin another location with `--registry-store` or `CHAT_WATCHDOG_REGISTRY_STORE`. Exactly one live process may own a store. Incompatible legacy databases are rejected, not silently migrated. Existing 0.1 deployments need an explicit inventory-preserving handover before stopping their in-memory registry.

Registry mode does not share one reanchor scope across conversations; single-conversation mode remains available for an explicit reanchor binding. Localhost is a trusted local-operator boundary, not signed proof of human authorization. Callers must retain requester/explicit-target policy; never infer authority from browser focus, a title, a previous assistant summary, or a recovered URL.

## Release and deployment gates

Build a wheel with `python -m build --wheel`, install it into a new stable virtual environment, and verify the installed module path/version independently of the checkout. Do not replace modules inside a live environment or point scheduled tasks at disposable worktrees. The Windows task should own the long-lived Python process directly with restart-on-failure settings, not launch a detached child and immediately report success. Record the old action, active inventory and exact package before handover; re-read `/health` and `/watches` afterward.

Tests include isolated process kill/restart, offline restoration, completion receipts, store ownership, withdrawal races, failure isolation and control responsiveness. They never send prompts to real ChatGPT conversations. Browser/Sidecar live acceptance and an external independent audit remain separate gates, not consequences of passing pytest.

When extension reload is actually needed, use the verified `chatgpt-conversation extension-update` workflow. Do not close the browser, clear pending operations or equate a reload ACK with readiness. Re-read the same extension/build, new instance and restored exact bindings afterward.

## Repository and recovery

The canonical repository is `user141514/watchdog`, created by the user. Its SSH remote is `git@github.com:user141514/watchdog.git`. Clone it normally:

```sh
git clone git@github.com:user141514/watchdog.git watchdog
cd watchdog
git fsck --full
```

The existing PC installation continues to use `C:/Users/Administrator/gitproject/chat-watchdog`; a new remote name does not require moving that checkout or reinstalling the runtime. The imported baseline was rebased onto the user's initial LICENSE commit without changing runtime or test files. The original pre-publication commit `ad697062820cd502f3376ffe055e7e7d6fad49e0` remains in the local backup branch and the archived bundle; it is a historical baseline, not the current remote HEAD.

The self-contained bundle recorded in the mymem closeout note remains an independent offline recovery option:

```sh
git clone /path/to/chat-watchdog-<commit>.bundle chat-watchdog
cd chat-watchdog
git fsck --full
```

The bundle restores the pre-publication baseline rather than tracking later remote commits. For current source use the canonical repository above and verify its remote HEAD. Preserve existing history; do not force-push or substitute the earlier proposed but unused `user141514/chat-watchdog` repository name.
