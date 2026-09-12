# chat-watchdog + reanchor

Versioned source for the existing, bounded PC watchdog deployment. This is not a new implementation and does not modify official DevSpace. The Python watchdog and the colocated Node reanchor package are kept together because the verified PC deployment already consumes this layout.

## Scope and invariants

The watchdog observes one explicitly selected, already-open ChatGPT conversation through an independently owned OMP Browser Relay. Reanchor retains directives, observations, context identity, delivery identity and nonce-bound model checkpoints. A model checkpoint is advisory; it is not independent proof of semantic completion.

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

The OMP relay, existing browser-extension trust and selected browser conversation must be verified independently before launching a watch. Consult `python -m chat_watchdog --help` and `node reanchor/bin/reanchor.mjs --help` for the installed command contracts. Do not copy a different host's live scope or conversation IDs into a new deployment.

## Portable Git recovery

A self-contained Git bundle can preserve this repository while creation of a dedicated remote is blocked. Clone the exact bundle recorded in the mymem closeout note:

```sh
git clone /path/to/chat-watchdog-<commit>.bundle chat-watchdog
cd chat-watchdog
git fsck --full
```

An archived bundle is a versioned recovery artifact, not an assertion that a standalone GitHub repository exists. Configure a dedicated `origin` only after that repository is actually created and verified. Do not force-push or replace a repository with a similar name.
