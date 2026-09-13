from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import logging
import os
from pathlib import Path
import socket
import sys
import time

from .agent_runner import AgentPool
from .cli import parse_agent_command
from .reanchor_bridge import ReanchorBridge, ReanchorCli
from .registry import WATCH_STATES, WatchEntry, WatchRegistry
from .registry_daemon import WatchDaemon
from .relay_page import RelayChatGPTPage
from .supervisor import Supervisor


def default_registry_path() -> Path:
    configured = os.environ.get("CHAT_WATCHDOG_REGISTRY")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".chat-watchdog" / "watch-registry.sqlite3"


@dataclass
class _LiveRuntime:
    page: RelayChatGPTPage
    supervisor: Supervisor

    def close(self) -> None:
        self.supervisor.close()
        self.page.close()


def _add_common_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--relay-url",
        default=os.environ.get("CHAT_WATCHDOG_RELAY_URL", "http://127.0.0.1:9224"),
        help="OMP browser-relay HTTP endpoint",
    )
    parser.add_argument("--poll-seconds", type=float, default=60.0)
    parser.add_argument("--agent", default="omp -p")
    parser.add_argument("--fallback-agent", action="append", default=[])
    parser.add_argument("--agent-probe-seconds", type=float, default=1.0)
    parser.add_argument("--recovery-timeout-seconds", type=float, default=120.0)
    parser.add_argument(
        "--reanchor-heartbeat-seconds",
        type=float,
        default=float(os.environ.get("CHAT_WATCHDOG_REANCHOR_HEARTBEAT_SECONDS", "900")),
    )
    parser.add_argument(
        "--reanchor-store",
        default=os.environ.get("CHAT_WATCHDOG_REANCHOR_STORE"),
    )
    parser.add_argument(
        "--reanchor-cli",
        default=os.environ.get("CHAT_WATCHDOG_REANCHOR_CLI"),
    )
    parser.add_argument(
        "--reanchor-owner",
        default=os.environ.get("CHAT_WATCHDOG_REANCHOR_OWNER", "coordinator"),
    )
    parser.add_argument(
        "--reanchor-context-root",
        default=os.environ.get("CHAT_WATCHDOG_REANCHOR_CONTEXT_ROOT"),
    )
    parser.add_argument(
        "--reanchor-node",
        default=os.environ.get("CHAT_WATCHDOG_REANCHOR_NODE", "node"),
    )
    parser.add_argument("--verbose", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chat-watchdog-registry",
        description="Manage and run a durable registry of exact ChatGPT conversation watches.",
    )
    parser.add_argument(
        "--store",
        default=str(default_registry_path()),
        help="SQLite watch registry path",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    add = subparsers.add_parser("add", help="register one exact ChatGPT conversation")
    add.add_argument("watch_id")
    add.add_argument("target_url")
    add.add_argument("--reanchor-scope")
    add.add_argument("--reanchor-epoch")

    list_parser = subparsers.add_parser("list", help="list registered watches")
    list_parser.add_argument("--json", action="store_true")
    list_parser.add_argument("--state", choices=sorted(WATCH_STATES))

    for command, help_text in (
        ("pause", "pause a watch without deleting it"),
        ("arm", "activate a paused or completed watch"),
        ("remove", "remove a watch"),
    ):
        item = subparsers.add_parser(command, help=help_text)
        item.add_argument("watch_id")

    run = subparsers.add_parser("run", help="run the dynamic registry daemon")
    _add_common_run_arguments(run)
    return parser


def _print_entry(entry: WatchEntry) -> None:
    print(f"{entry.watch_id}\t{entry.state}\t{entry.target_url}")


def _build_reanchor(entry: WatchEntry, args) -> ReanchorBridge | None:
    if entry.reanchor_scope is None:
        return None
    if not args.reanchor_store or not args.reanchor_cli:
        raise ValueError(
            f"watch {entry.watch_id!r} requires --reanchor-store and --reanchor-cli"
        )
    context = {
        "host": socket.gethostname(),
        "root": args.reanchor_context_root or os.getcwd(),
        "revision": None,
        "epoch": entry.reanchor_epoch,
    }
    return ReanchorBridge(
        cli=ReanchorCli(
            store=args.reanchor_store,
            cli_path=args.reanchor_cli,
            node_executable=args.reanchor_node,
        ),
        scope=entry.reanchor_scope,
        owner=args.reanchor_owner,
        context=context,
    )


def _run_daemon(registry: WatchRegistry, args) -> int:
    if args.poll_seconds <= 0:
        raise ValueError("--poll-seconds must be > 0")
    if args.agent_probe_seconds < 0:
        raise ValueError("--agent-probe-seconds must be >= 0")
    if args.recovery_timeout_seconds <= 0:
        raise ValueError("--recovery-timeout-seconds must be > 0")
    if args.reanchor_heartbeat_seconds <= 0:
        raise ValueError("--reanchor-heartbeat-seconds must be > 0")

    for entry in registry.list(state="active"):
        if entry.reanchor_scope is not None and (
            not args.reanchor_store or not args.reanchor_cli
        ):
            raise ValueError(
                f"watch {entry.watch_id!r} has reanchor binding but daemon reanchor installation is incomplete"
            )

    specs = [parse_agent_command(args.agent)]
    specs.extend(parse_agent_command(value) for value in args.fallback_agent)
    pool = AgentPool(specs, startup_probe_seconds=args.agent_probe_seconds)

    def runtime_factory(entry: WatchEntry) -> _LiveRuntime:
        page = RelayChatGPTPage.connect(args.relay_url, entry.target_url)
        try:
            supervisor = Supervisor(
                page,
                pool,
                recovery_timeout_seconds=args.recovery_timeout_seconds,
                heartbeat_seconds=args.reanchor_heartbeat_seconds,
                reanchor=_build_reanchor(entry, args),
            )
        except Exception:
            page.close()
            raise
        return _LiveRuntime(page=page, supervisor=supervisor)

    daemon = WatchDaemon(registry, runtime_factory)
    logging.info("watch registry daemon started: %s", registry.path)
    try:
        while True:
            results = daemon.step()
            for watch_id, result in results.items():
                logging.info("watchdog state: %s=%s", watch_id, result.value)
            time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        logging.info("registry daemon stopped by user")
        return 130
    finally:
        daemon.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        with WatchRegistry(args.store) as registry:
            if args.command == "add":
                entry = registry.add(
                    args.watch_id,
                    args.target_url,
                    reanchor_scope=args.reanchor_scope,
                    reanchor_epoch=args.reanchor_epoch,
                )
                _print_entry(entry)
                return 0
            if args.command == "list":
                entries = registry.list(state=args.state)
                if args.json:
                    print(json.dumps([asdict(entry) for entry in entries], ensure_ascii=False))
                else:
                    for entry in entries:
                        _print_entry(entry)
                return 0
            if args.command == "pause":
                _print_entry(registry.set_state(args.watch_id, "paused"))
                return 0
            if args.command == "arm":
                _print_entry(registry.set_state(args.watch_id, "active"))
                return 0
            if args.command == "remove":
                if not registry.remove(args.watch_id):
                    print(f"watch not found: {args.watch_id}", file=sys.stderr)
                    return 1
                print(args.watch_id)
                return 0
            if args.command == "run":
                return _run_daemon(registry, args)
    except (KeyError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2

    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
