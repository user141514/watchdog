# chat-watchdog + reanchor

Versioned source for the existing, bounded PC watchdog deployment. This is not a new implementation and does not modify official DevSpace. The Python watchdog and the colocated Node reanchor package are kept together because the verified PC deployment already consumes this layout.

## Scope and invariants

The legacy `chat-watchdog` command observes one explicitly selected, already-open ChatGPT conversation through an independently owned OMP Browser Relay. The `chat-watchdog-registry` command adds a durable explicit set of exact conversation watches above the same per-conversation Supervisor, so new watches can be added or paused without restarting the daemon. Reanchor retains directives, observations, context identity, delivery identity and nonce-bound model checkpoints. A model checkpoint is advisory; it is not independent proof of semantic completion.

- Do not select a target by a partial or ambiguous match when multiple tabs qualify.
- Preserve the exact scope, context epoch, packet nonce and delivered message on recovery. Do not resend merely because a tool response or process was lost.
- A send ACK is not completion. `disabled=false` does not override `aria-disabled=true`.
- Absence of a stop button does not prove finality. Ambiguous or stale page state must remain blocked.
- A source installation, regression pass, runtime process and real browser acceptance are separate claims.
- No continuous watcher, boot-time service, automatic browser refresh policy or 24/7 relay is installed by this repository.
- Keep live conversation fixtures, credentials, event stores, browser profiles and raw execution logs outside source control.

## Provenance

The imported baseline is the already-verified PC source under `C:/Users/Administrator/gitproject/chat-watchdog`. Its donor directories were `E:/Dev/chat-watchdog` and `E:/Dev/reanchor` on LAPTOP-M7FG2GG1; neither donor directory had Git history. This repository therefore begins with a source snapshot, not fabricated upstream history.

The PC source includes the previously accepted submit/finality corrections and the original nonce-validation contract. On 2026-09-12 the retained acceptance store was re-read as COMPLETE with no pending packet; the successful process was re-read as terminal, succeeded, exit 0 and quiescent. That controlled acceptance was not rerun for this import.

The available PC regression suite is 11 Python tests and 5 Node protocol tests. It is not the donor's complete historical suite. Host-specific receipts remain in the local `DEPLOYMENT_STATUS.md` and in the separately versioned mymem project records.

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

The registry stores exact canonical ChatGPT conversation URLs in SQLite. It never follows browser focus and never silently retargets a watch ID.

```powershell
chat-watchdog-registry add agent-main https://chatgpt.com/g/g-p-example-agent/c/example-conversation
chat-watchdog-registry list --json
chat-watchdog-registry run --relay-url http://127.0.0.1:9224 --poll-seconds 60
```

While the daemon is running, `add`, `pause`, `arm`, and `remove` update the same SQLite registry transactionally. The daemon reconciles those changes on its next cycle. A watch that reaches `DONE` is persisted as `completed` and will not be recreated until an explicit `arm`.

The default registry is `~/.chat-watchdog/watch-registry.sqlite3`; override it with `--store` or `CHAT_WATCHDOG_REGISTRY`. Reanchor remains optional and target-bound: add both `--reanchor-scope` and `--reanchor-epoch` to a watch, then supply the daemon's existing `--reanchor-store` and `--reanchor-cli` installation arguments. The registry does not retarget reanchor scopes.

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
