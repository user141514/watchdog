from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import shlex
import socket
import sys
from threading import Thread
import time

from .agent_runner import AgentPool, AgentSpec
from .reanchor_bridge import ReanchorBridge, ReanchorCli
from .registry import RegistrationRejected, WatchRegistry, conversation_id_from_url, create_control_server
from .relay_page import RelayChatGPTPage
from .intent_client import SidecarIntentClient, DEFAULT_INTENT_URL
from .progress_liveness import MymemLiteStore
from .state_client import SidecarStateClient, StateProtocolError, StateUnavailable, sibling_state_endpoint
from .supervisor import StepResult, Supervisor


def parse_agent_command(value: str) -> AgentSpec:
    argv = shlex.split(value, posix=os.name != "nt")
    if os.name == "nt":
        argv = [
            part[1:-1]
            if len(part) >= 2 and part[0] == part[-1] and part[0] in {'"', "'"}
            else part
            for part in argv
        ]
    if not argv:
        raise ValueError("agent command must not be empty")
    if not any("{prompt}" in part for part in argv):
        argv.append("{prompt}")
    return AgentSpec(name=os.path.basename(argv[0]), argv_template=tuple(argv))


def build_reanchor_bridge(args) -> ReanchorBridge | None:
    required = {
        "--reanchor-store": args.reanchor_store,
        "--reanchor-scope": args.reanchor_scope,
        "--reanchor-cli": args.reanchor_cli,
        "--reanchor-epoch": args.reanchor_epoch,
    }
    present = {name: value for name, value in required.items() if value}
    if not present:
        return None
    if len(present) != len(required):
        missing = ", ".join(name for name, value in required.items() if not value)
        raise ValueError(
            "reanchor requires all of --reanchor-store, --reanchor-scope, "
            f"--reanchor-cli and --reanchor-epoch; missing: {missing}"
        )
    context = {
        "host": socket.gethostname(),
        "root": args.reanchor_context_root or os.getcwd(),
        "revision": None,
        "epoch": args.reanchor_epoch,
    }
    return ReanchorBridge(
        cli=ReanchorCli(
            store=args.reanchor_store,
            cli_path=args.reanchor_cli,
            node_executable=args.reanchor_node,
        ),
        scope=args.reanchor_scope,
        owner=args.reanchor_owner,
        context=context,
    )


class _ObservationPage:
    """Managed supervisors receive observation capability, not Relay mutation methods."""
    def __init__(self, page):
        self._page = page
    @property
    def target_url(self):
        return self._page.target_url
    def snapshot(self):
        return self._page.snapshot()


def _managed_intent_endpoint(args):
    if args.legacy_direct_send:
        if args.intent_url or args.send_admission_url:
            raise ValueError('legacy direct mode cannot claim managed admission or mailbox')
        return None
    if args.reanchor_store or args.reanchor_scope or args.reanchor_cli or args.reanchor_epoch:
        raise ValueError('direct-write reanchor requires explicit --legacy-direct-send')
    endpoint = args.intent_url
    if args.send_admission_url:
        if endpoint or not args.send_admission_url.endswith('/internal/send-admission'):
            raise ValueError('choose one Sidecar owner endpoint')
        endpoint = args.send_admission_url.removesuffix('/internal/send-admission') + '/internal/conversation-intents'
    return endpoint or DEFAULT_INTENT_URL


def build_intent_client(args):
    endpoint = _managed_intent_endpoint(args)
    return None if endpoint is None else SidecarIntentClient(endpoint)


def build_state_client(args):
    endpoint = _managed_intent_endpoint(args)
    return None if endpoint is None else SidecarStateClient(sibling_state_endpoint(endpoint))


def validate_managed_registration(state_client, target_url: str) -> None:
    try:
        state = state_client.read(target_url)
    except (StateUnavailable, StateProtocolError) as error:
        raise RegistrationRejected("NOT_MOUNTABLE_MANAGED", str(error)) from error
    writer = state.to_dict().get("writer") or {}
    if writer.get("mode") != "managed":
        raise RegistrationRejected("NOT_MOUNTABLE_MANAGED", "writer_mode_mismatch")


class _SupervisorWatcher:
    def __init__(self, page: RelayChatGPTPage, supervisor: Supervisor) -> None:
        self.page = page
        self.supervisor = supervisor
        self._state = "active"

    @property
    def should_stop(self) -> bool:
        return self.supervisor.should_stop

    @property
    def state(self) -> str:
        return self._state

    @property
    def diagnostics(self) -> dict:
        return dict(self.supervisor.diagnostics)

    @property
    def completion_text(self) -> str | None:
        try:
            return self.page.snapshot().assistant_text
        except Exception:
            return None

    def step(self) -> object:
        result = self.supervisor.step()
        if result is StepResult.NEED_INPUT:
            self._state = "need_input"
        elif result in {StepResult.WAITING, StepResult.USER_TURN_PENDING}:
            self._state = "waiting"
        elif result in {StepResult.BLOCKED, StepResult.RECOVERY_UNAVAILABLE}:
            self._state = "blocked"
        elif result is StepResult.DELIVERY_UNCERTAIN:
            self._state = "delivery_uncertain"
        else:
            self._state = "active"
        logging.info("watchdog %s state: %s", self.page.target_url, result.value)
        return result

    def close(self) -> None:
        self.supervisor.close()
        self.page.close()


def _run_registry_mode(args, pool: AgentPool, intent_client, state_client) -> int:
    if args.reanchor_store or args.reanchor_scope or args.reanchor_cli or args.reanchor_epoch:
        raise SystemExit("registry mode does not share one reanchor scope across multiple conversations")

    progress_store = MymemLiteStore(args.mymem_lite_dir)

    def create_watcher(target_url: str) -> _SupervisorWatcher:
        conversation_id = conversation_id_from_url(target_url)
        page = RelayChatGPTPage.connect(args.relay_url, f"/c/{conversation_id}")
        supervisor = Supervisor(
            _ObservationPage(page) if intent_client else page,
            pool,
            recovery_timeout_seconds=args.recovery_timeout_seconds,
            heartbeat_seconds=args.reanchor_heartbeat_seconds,
            reanchor=None,
            intent_client=intent_client,
            state_client=state_client,
            progress_store=progress_store,
            progress_id=conversation_id,
            active_stall_seconds=args.active_stall_seconds,
        )
        return _SupervisorWatcher(page, supervisor)

    registration_preflight = None
    if state_client is not None:
        registration_preflight = lambda target_url: validate_managed_registration(state_client, target_url)
    registry = WatchRegistry(
        create_watcher,
        store_path=args.registry_store,
        connect_on_register=False,
        registration_preflight=registration_preflight,
        progress_store=progress_store,
    )
    try:
        server = create_control_server(
            registry, args.registry_host, args.registry_port,
            stale_after=max(30.0, args.poll_seconds * 3),
        )
    except BaseException:
        registry.close()
        raise
    control = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1},
                     name="watchdog-control", daemon=True)
    control.start()
    logging.info(
        "durable watch registry on http://%s:%d; store=%s instance=%s",
        args.registry_host, server.server_address[1], args.registry_store, registry.instance_id,
    )
    try:
        while control.is_alive():
            started = time.monotonic()
            try:
                registry.step_all()
            except Exception:
                # Storage failure stays observable and closes the send gate; retry
                # storage on the next poll without losing desired registrations.
                logging.exception("watch registry polling failed; retaining desired watches")
            time.sleep(max(0.01, args.poll_seconds - (time.monotonic() - started)))
        raise RuntimeError("watchdog control server unexpectedly stopped")
    except KeyboardInterrupt:
        logging.info("stopped by user")
        return 130
    finally:
        server.shutdown()
        control.join(timeout=5)
        server.server_close()
        registry.close()


def _default_state_root() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))


def default_registry_store() -> str:
    # Deliberately separate from the incompatible pre-registry compatibility DB.
    return str(_default_state_root() / "chat-watchdog" / "registry-v2.sqlite3")


def default_mymem_lite_dir() -> str:
    return str(_default_state_root() / "chat-watchdog" / "mymem-lite")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chat-watchdog",
        description="Watch one already-open ChatGPT conversation and keep unfinished work moving.",
    )
    parser.add_argument(
        "--relay-url",
        default=os.environ.get("CHAT_WATCHDOG_RELAY_URL", "http://127.0.0.1:9224"),
        help="OMP browser-relay HTTP endpoint",
    )
    parser.add_argument(
        "--match-url",
        default="chatgpt.com",
        help="substring that must uniquely identify the existing ChatGPT tab",
    )
    parser.add_argument(
        "--send-admission-url",
        default=os.environ.get("CHAT_WATCHDOG_SEND_ADMISSION_URL"),
        help="optional Sidecar localhost send-admission endpoint; enables shared pacing",
    )
    parser.add_argument('--intent-url', default=os.environ.get('CHAT_WATCHDOG_INTENT_URL'), help='Sidecar localhost intent owner')
    parser.add_argument('--legacy-direct-send', action='store_true', help='explicit standalone opt-out; no single-writer guarantee')
    parser.add_argument(
        "--registry-port",
        type=int,
        default=None,
        help="enable dynamic watch registry on localhost at this port",
    )
    parser.add_argument(
        "--registry-store",
        default=os.environ.get("CHAT_WATCHDOG_REGISTRY_STORE") or default_registry_store(),
        help="durable desired registrations and receipts; exactly one process owns this SQLite file",
    )
    parser.add_argument(
        "--registry-host",
        default="127.0.0.1",
        help="registry control host; localhost only",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=60.0,
        help="poll interval; default: 60",
    )
    parser.add_argument(
        "--active-stall-seconds",
        type=float,
        default=float(os.environ.get("CHAT_WATCHDOG_ACTIVE_STALL_SECONDS", "300")),
        help="continuous observable active period without progress before one stop recovery; default: 300",
    )
    parser.add_argument(
        "--mymem-lite-dir",
        default=os.environ.get("CHAT_WATCHDOG_MYMEM_LITE_DIR") or default_mymem_lite_dir(),
        help="ephemeral append-only progress pulse directory",
    )
    parser.add_argument(
        "--agent",
        default="omp -p",
        help='preferred recovery agent command; default: "omp -p"',
    )
    parser.add_argument(
        "--fallback-agent",
        action="append",
        default=[],
        help='additional recovery command in order, e.g. --fallback-agent "helper --browser"',
    )
    parser.add_argument(
        "--agent-probe-seconds",
        type=float,
        default=1.0,
        help="how long a recovery process must stay alive before acquiring the lease",
    )
    parser.add_argument(
        "--recovery-timeout-seconds",
        type=float,
        default=120.0,
        help="max time a leased recovery agent may fail to advance the conversation",
    )
    parser.add_argument(
        "--reanchor-heartbeat-seconds",
        type=float,
        default=float(os.environ.get("CHAT_WATCHDOG_REANCHOR_HEARTBEAT_SECONDS", "900")),
        help="stale-progress interval before arming a safe-boundary re-anchor; default: 900",
    )
    parser.add_argument(
        "--reanchor-store",
        default=os.environ.get("CHAT_WATCHDOG_REANCHOR_STORE"),
        help="existing durable reanchor store; enables reanchor only with all required options",
    )
    parser.add_argument(
        "--reanchor-scope",
        default=os.environ.get("CHAT_WATCHDOG_REANCHOR_SCOPE"),
        help="pre-initialized reanchor scope bound to the watched conversation",
    )
    parser.add_argument(
        "--reanchor-cli",
        default=os.environ.get("CHAT_WATCHDOG_REANCHOR_CLI"),
        help="path to reanchor bin/reanchor.mjs",
    )
    parser.add_argument(
        "--reanchor-epoch",
        default=os.environ.get("CHAT_WATCHDOG_REANCHOR_EPOCH"),
        help="explicit stable runtime/context epoch for this watched thread",
    )
    parser.add_argument(
        "--reanchor-owner",
        default=os.environ.get("CHAT_WATCHDOG_REANCHOR_OWNER", "coordinator"),
        help="coordinator owner id; default: coordinator",
    )
    parser.add_argument(
        "--reanchor-context-root",
        default=os.environ.get("CHAT_WATCHDOG_REANCHOR_CONTEXT_ROOT"),
        help="context root recorded in reanchor evidence; default: current directory",
    )
    parser.add_argument(
        "--reanchor-node",
        default=os.environ.get("CHAT_WATCHDOG_REANCHOR_NODE", "node"),
        help="Node executable for reanchor CLI; default: node",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.poll_seconds <= 0:
        raise SystemExit("--poll-seconds must be > 0")
    if args.active_stall_seconds <= 0:
        raise SystemExit("--active-stall-seconds must be > 0")
    if args.agent_probe_seconds < 0:
        raise SystemExit("--agent-probe-seconds must be >= 0")
    if args.recovery_timeout_seconds <= 0:
        raise SystemExit("--recovery-timeout-seconds must be > 0")
    if args.reanchor_heartbeat_seconds <= 0:
        raise SystemExit("--reanchor-heartbeat-seconds must be > 0")
    if args.registry_port is not None and not 1 <= args.registry_port <= 65535:
        raise SystemExit("--registry-port must be between 1 and 65535")
    if args.registry_port is not None and args.legacy_direct_send:
        raise SystemExit(
            "durable registry requires Sidecar; --legacy-direct-send is single-conversation only"
        )

    try:
        intent_client = build_intent_client(args)
        state_client = build_state_client(args)
    except ValueError as error:
        raise SystemExit(str(error)) from error

    specs = [parse_agent_command(args.agent)]
    specs.extend(parse_agent_command(value) for value in args.fallback_agent)
    pool = AgentPool(specs, startup_probe_seconds=args.agent_probe_seconds)

    if args.registry_port is not None:
        return _run_registry_mode(args, pool, intent_client, state_client)

    try:
        reanchor = build_reanchor_bridge(args)
    except ValueError as error:
        raise SystemExit(str(error)) from error

    page = RelayChatGPTPage.connect(args.relay_url, args.match_url)
    supervisor = Supervisor(
        _ObservationPage(page) if intent_client else page,
        pool,
        recovery_timeout_seconds=args.recovery_timeout_seconds,
        heartbeat_seconds=args.reanchor_heartbeat_seconds,
        reanchor=reanchor,
        intent_client=intent_client,
        state_client=state_client,
    )

    logging.info("watching %s every %.1fs", page.target_url, args.poll_seconds)
    try:
        while not supervisor.should_stop:
            result = supervisor.step()
            logging.info("watchdog state: %s", result.value)
            if not supervisor.should_stop:
                time.sleep(args.poll_seconds)
        return 0
    except KeyboardInterrupt:
        logging.info("stopped by user")
        return 130
    finally:
        supervisor.close()
        page.close()


if __name__ == "__main__":
    raise SystemExit(main())
