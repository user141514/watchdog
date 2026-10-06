from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import shlex
import socket
import sys
from threading import Event, Thread
import time

from .agent_runner import AgentPool, AgentSpec
from .reanchor_bridge import ReanchorBridge, ReanchorCli
from .registry import RegistrationRejected, WatchRegistry, conversation_id_from_url, create_control_server
from .relay_page import RelayChatGPTPage
from .intent_client import SidecarIntentClient, DEFAULT_INTENT_URL
from .observation_client import SidecarObservationClient, SidecarObservationPage, sibling_observation_endpoint
from .progress_liveness import MymemLiteStore
from .state_client import SidecarStateClient, StateProtocolError, StateUnavailable, sibling_state_endpoint
from .supervisor import StepResult, Supervisor
from .simple_watchdog import (
    DEFAULT_SIMPLE_INTERVAL_SECONDS,
    SIMPLE_INACTIVITY_TIMEOUT_SECONDS,
    SimpleWatcher,
)
from .fixed_action_scheduler import WindowsFixedActionScheduler


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
        self._completion_text = None

    def bind_registration(self, registration_id):
        self.page.bind_registration(registration_id)

    @property
    def should_stop(self) -> bool:
        return False

    @property
    def state(self) -> str:
        return self._state

    @property
    def requires_owner_rebind(self) -> bool:
        return any(self.supervisor.diagnostics.get(key) == "watchdog_binding_required"
                   for key in ("intent_reason", "recovery_reason"))

    @property
    def diagnostics(self) -> dict:
        return dict(self.supervisor.diagnostics)

    @property
    def completion_text(self) -> str | None:
        return self._completion_text

    def step(self) -> object:
        if self.supervisor.should_stop:
            self._state = "done"
            return StepResult.DONE
        result = self.supervisor.step()
        self._completion_text = getattr(self.page, "completion_text", None)
        if result is StepResult.DONE:
            self._state = "done"
        elif result is StepResult.NEED_INPUT:
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


def _run_registry_mode(args, pool: AgentPool | None, intent_client, state_client) -> int:
    if args.reanchor_store or args.reanchor_scope or args.reanchor_cli or args.reanchor_epoch:
        raise SystemExit("registry mode does not share one reanchor scope across multiple conversations")

    if args.simple:
        progress_store = None
        poll_seconds = args.simple_interval_seconds

        endpoint = _managed_intent_endpoint(args)
        simple_intents = intent_client or SidecarIntentClient(endpoint)
        owner_client = simple_intents
        simple_observations = SidecarObservationClient(sibling_observation_endpoint(endpoint))

        def create_watcher(target_url: str) -> SimpleWatcher:
            return SimpleWatcher(
                target_url,
                liveness_timeout_seconds=args.simple_inactivity_seconds,
                observation_client=simple_observations,
                intent_client=simple_intents,
            )

        registration_preflight = None
    else:
        owner_client = intent_client
        if pool is None:
            raise RuntimeError("managed registry requires an agent pool")
        progress_store = MymemLiteStore(args.mymem_lite_dir)
        poll_seconds = args.poll_seconds

        def create_watcher(target_url: str) -> _SupervisorWatcher:
            conversation_id = conversation_id_from_url(target_url)
            if intent_client is not None:
                page = SidecarObservationPage(
                    target_url,
                    SidecarObservationClient(sibling_observation_endpoint(intent_client.endpoint)),
                    intent_client, refresh_on_snapshot=True,
                )
            else:
                page = RelayChatGPTPage.connect(args.relay_url, f"/c/{conversation_id}")
            supervisor = Supervisor(
                _ObservationPage(page) if intent_client else page,
                pool,
                recovery_timeout_seconds=args.recovery_timeout_seconds,
                heartbeat_seconds=args.reanchor_heartbeat_seconds,
                reanchor=None,
                intent_client=page if intent_client else None,
                state_client=page if intent_client else state_client,
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
        withdrawal_callback=owner_client.withdraw_watch if owner_client is not None else None,
        progress_store=progress_store,
    )
    fixed_action_scheduler = (
        _build_fixed_action_scheduler()
        if args.simple and args.fixed_action_scheduler and os.name == "nt"
        else None
    )
    wake_event = Event()
    try:
        server = create_control_server(
            registry, args.registry_host, args.registry_port,
            stale_after=max(30.0, poll_seconds * 3),
            wake=wake_event.set,
            register_projection=(
                fixed_action_scheduler.ensure_attached
                if fixed_action_scheduler is not None
                else None
            ),
        )
    except BaseException:
        registry.close()
        raise
    control = Thread(target=_serve_registry_control, args=(server, wake_event),
                     name="watchdog-control", daemon=True)
    control.start()
    logging.info(
        "durable watch registry on http://%s:%d; store=%s instance=%s",
        args.registry_host, server.server_address[1], args.registry_store, registry.instance_id,
    )
    try:
        while control.is_alive():
            _registry_poll_cycle(
                registry,
                wake_event,
                poll_seconds,
                is_control_alive=control.is_alive,
                fixed_action_scheduler=fixed_action_scheduler,
            )
        raise RuntimeError("watchdog control server unexpectedly stopped")
    except KeyboardInterrupt:
        logging.info("stopped by user")
        return 130
    finally:
        server.shutdown()
        control.join(timeout=5)
        server.server_close()
        registry.close()


def _serve_registry_control(server, wake_event) -> None:
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        # Empty membership has no timer; control shutdown must release its wait.
        wake_event.set()


def _registry_poll_cycle(registry, wake_event, poll_seconds: float, *,
                         is_control_alive=None, fixed_action_scheduler=None) -> None:
    # Clear before observing membership. A registration during the poll/check
    # leaves its wake set, so the subsequent wait cannot lose that change.
    wake_event.clear()
    if is_control_alive is not None and not is_control_alive():
        return
    started = time.monotonic()
    try:
        registry.step_all()
    except Exception:
        logging.exception("watch registry polling failed; retaining desired watches")
    if fixed_action_scheduler is not None:
        try:
            fixed_action_scheduler.reconcile(registry.list())
        except Exception:
            logging.exception(
                "fixed ACTION task projection failed; retaining desired watches for retry"
            )
    if not registry.has_scheduler_work():
        wake_event.wait()
        return
    remaining = max(0.01, poll_seconds - (time.monotonic() - started))
    wake_event.wait(timeout=remaining)


def _default_state_root() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))


def _build_fixed_action_scheduler() -> WindowsFixedActionScheduler:
    runtime_root = _default_state_root() / "chat-watchdog"
    release_dir = Path(__file__).resolve().parents[1]
    python = Path(sys.executable)
    windowless_python = python.with_name("pythonw.exe")
    if windowless_python.exists():
        python = windowless_python
    return WindowsFixedActionScheduler(
        runtime_root=runtime_root,
        release_dir=release_dir,
        python_executable=python,
    )


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
        help="exact conversation URL in Sidecar mode; unique tab substring in legacy mode",
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
        "--simple",
        action="store_true",
        help="use the mechanical ACTION/REVIEW watchdog through Sidecar; registry mode only",
    )
    parser.add_argument(
        "--simple-interval-seconds",
        type=float,
        default=DEFAULT_SIMPLE_INTERVAL_SECONDS,
        help="simple watchdog observation interval; default: 15 seconds",
    )
    parser.add_argument(
        "--simple-inactivity-seconds",
        type=float,
        default=SIMPLE_INACTIVITY_TIMEOUT_SECONDS,
        help="simple watchdog no-progress timeout before owner recovery; default: 900 seconds",
    )
    parser.add_argument(
        "--fixed-action-scheduler",
        action="store_true",
        help=(
            "project durable simple-registry membership into independent Windows "
            "fixed-action Scheduled Tasks; canonical production runtime only"
        ),
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
    if args.simple_interval_seconds <= 0:
        raise SystemExit("--simple-interval-seconds must be > 0")
    if args.simple_inactivity_seconds <= 0:
        raise SystemExit("--simple-inactivity-seconds must be > 0")
    if args.fixed_action_scheduler and not args.simple:
        raise SystemExit("--fixed-action-scheduler requires --simple")
    if args.fixed_action_scheduler and os.name != "nt":
        raise SystemExit("--fixed-action-scheduler is Windows-only")
    if args.simple:
        if args.registry_port is None:
            raise SystemExit("--simple requires --registry-port")
        if any((args.legacy_direct_send, args.reanchor_store, args.reanchor_scope,
                args.reanchor_cli, args.reanchor_epoch)):
            raise SystemExit("--simple requires Sidecar ownership; do not combine legacy/reanchor writers")
        return _run_registry_mode(args, None, None, None)

    if args.registry_port is None and not args.legacy_direct_send:
        raise SystemExit("managed Sidecar Watchdog requires --registry-port and an explicit durable registration")

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

    if intent_client is not None:
        try:
            page = SidecarObservationPage(
                args.match_url,
                SidecarObservationClient(sibling_observation_endpoint(intent_client.endpoint)),
                intent_client, refresh_on_snapshot=True,
            )
        except ValueError as error:
            raise SystemExit("managed --match-url must be an exact ChatGPT conversation URL") from error
    else:
        page = RelayChatGPTPage.connect(args.relay_url, args.match_url)
    supervisor = Supervisor(
        _ObservationPage(page) if intent_client else page,
        pool,
        recovery_timeout_seconds=args.recovery_timeout_seconds,
        heartbeat_seconds=args.reanchor_heartbeat_seconds,
        reanchor=reanchor,
        intent_client=intent_client,
        state_client=page if intent_client else state_client,
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
