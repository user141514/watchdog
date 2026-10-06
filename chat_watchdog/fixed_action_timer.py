from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Callable
from urllib.parse import urlsplit
from urllib.request import urlopen

from .model import Phase, PromptDelivery, is_done, is_need_input
from .registry import conversation_id_from_url
from .relay_page import RelayChatGPTPage


FIXED_ACTION_PROMPT = (
    "这是 15 分钟独立固定兜底。直接继续执行当前任务，从已经完成的内容直接往下推进；"
    "不要 REVIEW，不要等待监督链，不要重复已完成步骤。"
    "开始前快速核对真实进度、当前卡点和目标漂移，然后直接执行下一个最小可验证步骤；"
    "如果当前步骤已经完成，就继续下一个实际步骤。"
    "如果后续需要通过 Orca / orca-sub 启动新的智能体，必须使用 OMP，不要使用 Claude；OMP 不可用时不要自动降级到 Claude。"
    "如果整个任务已经真正完成，请在回复最后单独输出 SUPERVISOR_DONE。"
    "如果继续需要用户手动操作、登录、授权、确认或补充信息，请说明需要的动作，"
    "并在回复最后单独输出 [SUPERVISOR_STATE: NEED_INPUT]；不要自行假定用户已经完成。"
    "如果还没完成且不需要用户介入，就继续实际推进任务。"
)


@dataclass(frozen=True)
class TimerConfig:
    target_url: str
    relay_url: str = "http://127.0.0.1:9224"
    acceptance_timeout_seconds: float = 10.0
    interval_minutes: int = 15
    prompt: str = FIXED_ACTION_PROMPT

    def __post_init__(self) -> None:
        conversation_id_from_url(self.target_url)
        parsed = urlsplit(self.relay_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("relay_url must be a local HTTP endpoint")
        if (
            isinstance(self.acceptance_timeout_seconds, bool)
            or not isinstance(self.acceptance_timeout_seconds, (int, float))
            or not math.isfinite(float(self.acceptance_timeout_seconds))
            or float(self.acceptance_timeout_seconds) <= 0
        ):
            raise ValueError("acceptance_timeout_seconds must be finite and positive")
        if (
            isinstance(self.interval_minutes, bool)
            or not isinstance(self.interval_minutes, int)
            or not 1 <= self.interval_minutes <= 1440
        ):
            raise ValueError("interval_minutes must be an integer between 1 and 1440")
        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise ValueError("prompt must be non-empty")


def load_config(path: str | Path) -> TimerConfig:
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("timer config must be an object")
    allowed = {
        "target_url",
        "relay_url",
        "acceptance_timeout_seconds",
        "interval_minutes",
        "prompt",
    }
    extra = set(value) - allowed
    if extra:
        raise ValueError("unknown timer config fields: " + ", ".join(sorted(extra)))
    if not isinstance(value.get("target_url"), str):
        raise ValueError("target_url is required")
    return TimerConfig(
        target_url=value["target_url"],
        relay_url=value.get("relay_url", "http://127.0.0.1:9224"),
        acceptance_timeout_seconds=value.get("acceptance_timeout_seconds", 10.0),
        interval_minutes=value.get("interval_minutes", 15),
        prompt=value.get("prompt", FIXED_ACTION_PROMPT),
    )


def _fetch_json(url: str) -> object:
    with urlopen(url, timeout=2.0) as response:
        return json.load(response)


def _target_present(payload: object, target_url: str) -> bool:
    if not isinstance(payload, list):
        return False
    expected = conversation_id_from_url(target_url)
    for item in payload:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            continue
        try:
            if conversation_id_from_url(item["url"]) == expected:
                return True
        except ValueError:
            continue
    return False


def _start_relay(config: TimerConfig) -> None:
    parsed = urlsplit(config.relay_url)
    port = parsed.port or 80
    if os.name == "nt":
        omp = Path.home() / ".local" / "bin" / "omp.cmd"
        if not omp.is_file():
            found = shutil.which("omp.cmd") or shutil.which("omp")
            if not found:
                raise RuntimeError("omp browser-relay command unavailable")
            omp = Path(found)
        command = subprocess.list2cmdline(
            [str(omp), "browser-relay", "-p", str(port)]
        )
        argv = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", command]
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
        runtime_root = Path(
            os.environ.get(
                "LOCALAPPDATA",
                str(Path.home() / "AppData" / "Local"),
            )
        ) / "chat-watchdog"
    else:
        found = shutil.which("omp")
        if not found:
            raise RuntimeError("omp browser-relay command unavailable")
        argv = [found, "browser-relay", "-p", str(port)]
        creationflags = 0
        runtime_root = Path(
            os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))
        ) / "chat-watchdog"

    runtime_root.mkdir(parents=True, exist_ok=True)
    stdout_path = runtime_root / f"omp-relay-{port}.stdout.log"
    stderr_path = runtime_root / f"omp-relay-{port}.stderr.log"
    with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            creationflags=creationflags,
        )


def _ensure_relay_target(
    config: TimerConfig,
    *,
    timeout_seconds: float = 10.0,
    fetch_json: Callable[[str], object] = _fetch_json,
    start_relay: Callable[[TimerConfig], None] = _start_relay,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    target_list_url = config.relay_url.rstrip("/") + "/json/list"
    deadline = clock() + timeout_seconds
    relay_start_attempted = False
    while True:
        try:
            payload = fetch_json(target_list_url)
        except OSError:
            if not relay_start_attempted:
                start_relay(config)
                relay_start_attempted = True
        else:
            if _target_present(payload, config.target_url):
                return

        now = clock()
        if now >= deadline:
            raise RuntimeError("relay_target_unavailable")
        sleep(min(1.0, max(0.0, deadline - now)))


def _result_from_delivery(delivery: PromptDelivery) -> dict[str, object]:
    if delivery.accepted:
        return {
            "status": "sent",
            "message_id": delivery.message_id,
        }
    if delivery.uncertain:
        return {"status": "uncertain", "reason": delivery.reason or "delivery_uncertain"}
    if delivery.stale:
        return {"status": "skipped", "reason": delivery.reason or "turn_changed"}
    return {"status": "skipped", "reason": delivery.reason or "direct_send_rejected"}


def run_once(
    config: TimerConfig,
    *,
    page_factory: Callable[[str, str], RelayChatGPTPage] = RelayChatGPTPage.connect,
) -> dict[str, object]:
    timeout = float(config.acceptance_timeout_seconds)
    page = page_factory(config.relay_url, config.target_url)
    try:
        snapshot = page.snapshot()
        if not snapshot.turn_key:
            return {"status": "skipped", "reason": "missing_assistant_turn"}

        tail = (snapshot.assistant_text or "")[-4096:]
        if is_need_input(tail):
            return {"status": "skipped", "reason": "need_input"}
        if is_done(tail):
            return {"status": "skipped", "reason": "done"}

        if snapshot.interaction_required or snapshot.user_turn_pending:
            return {"status": "skipped", "reason": "human_gate"}

        if snapshot.phase in (Phase.THINKING, Phase.RESPONDING):
            delivery = page.send_simple_continue(
                config.prompt,
                snapshot.turn_key,
                acceptance_timeout=max(timeout, 40.0),
            )
            if not delivery.accepted and not delivery.uncertain and not delivery.stale:
                delivery = page.recover_stalled_active(
                    config.prompt,
                    snapshot.turn_key,
                    stop_timeout=timeout,
                    acceptance_timeout=max(timeout, 40.0),
                )
        elif snapshot.phase in (Phase.BLOCKED, Phase.FINISHED):
            delivery = page.send_continue(
                config.prompt,
                snapshot.turn_key,
                acceptance_timeout=timeout,
            )
        else:
            return {"status": "skipped", "reason": f"unsupported_phase:{snapshot.phase.value}"}
        return _result_from_delivery(delivery)
    finally:
        page.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Send one Sidecar-free fixed ACTION prompt through Relay/CDP."
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        _ensure_relay_target(config)
        result = run_once(config)
    except Exception as error:
        result = {
            "status": "error",
            "reason": type(error).__name__,
            "message": str(error)[:2000],
        }
        print(json.dumps(result, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    if result.get("status") == "sent":
        return 0
    if result.get("status") == "skipped" and result.get("reason") in {
        "done",
        "need_input",
        "human_gate",
    }:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
