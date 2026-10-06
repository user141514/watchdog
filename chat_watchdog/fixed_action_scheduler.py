from __future__ import annotations

import csv
import json
from pathlib import Path
import subprocess
from typing import Iterable

from .fixed_action_timer import FIXED_ACTION_PROMPT, TimerConfig, load_config
from .registry import conversation_id_from_url


TASK_PREFIX = "ChatGPT Fixed ACTION - "
LEGACY_V2_PREFIX = "ChatGPT Fixed ACTION V2 - "
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class WindowsFixedActionScheduler:
    """Project durable Watchdog membership into independent Windows tasks.

    Registry membership remains authoritative. Scheduled tasks are disposable
    projections: they can be rebuilt from the desired watch set and execute the
    fixed-action route without Sidecar or the normal ACTION/REVIEW state machine.
    """

    def __init__(
        self,
        *,
        runtime_root: str | Path,
        release_dir: str | Path,
        python_executable: str | Path,
        runner=subprocess.run,
    ) -> None:
        self.runtime_root = Path(runtime_root)
        self.release_dir = Path(release_dir)
        python = Path(python_executable)
        windowless_python = python.with_name("pythonw.exe")
        self.python_executable = windowless_python if windowless_python.exists() else python
        self._runner = runner
        self.config_dir = self.runtime_root / "fixed-action-timer"
        self.launcher_path = self.runtime_root / "fixed-action-current.pyw"
        self.legacy_launcher_path = self.runtime_root / "fixed-action-current.cmd"
        self._applied: dict[str, tuple[str, str, int]] = {}

    @staticmethod
    def task_name(target_url: str) -> str:
        return TASK_PREFIX + conversation_id_from_url(target_url)

    def config_path(self, target_url: str) -> Path:
        return self.config_dir / f"{conversation_id_from_url(target_url)}.json"

    def log_path(self, target_url: str) -> Path:
        return self.config_dir / f"{conversation_id_from_url(target_url)[:8]}.log"

    def _load_or_default(self, target_url: str) -> TimerConfig:
        path = self.config_path(target_url)
        if path.exists():
            current = load_config(path)
            prompt = (
                FIXED_ACTION_PROMPT
                if current.prompt.startswith("这是 15 分钟独立固定兜底。")
                else current.prompt
            )
            return TimerConfig(
                target_url=target_url,
                relay_url=current.relay_url,
                acceptance_timeout_seconds=current.acceptance_timeout_seconds,
                interval_minutes=current.interval_minutes,
                prompt=prompt,
            )
        return TimerConfig(target_url=target_url, prompt=FIXED_ACTION_PROMPT)

    def _write_config(self, config: TimerConfig) -> Path:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        path = self.config_path(config.target_url)
        payload = {
            "target_url": config.target_url,
            "relay_url": config.relay_url,
            "acceptance_timeout_seconds": config.acceptance_timeout_seconds,
            "interval_minutes": config.interval_minutes,
            "prompt": config.prompt,
        }
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(path)
        return path

    def _launcher_content(self) -> str:
        release = repr(str(self.release_dir))
        config_root = repr(str(self.config_dir))
        return (
            "from __future__ import annotations\n"
            "from contextlib import redirect_stderr, redirect_stdout\n"
            "import os\n"
            "from pathlib import Path\n"
            "import sys\n\n"
            f"release = Path({release})\n"
            f"config_root = Path({config_root})\n"
            "conversation_id = sys.argv[1] if len(sys.argv) == 2 else \"\"\n"
            "if not conversation_id:\n"
            "    raise SystemExit(2)\n"
            "config_path = config_root / f\"{conversation_id}.json\"\n"
            "log_path = config_root / f\"{conversation_id}.log\"\n"
            "config_root.mkdir(parents=True, exist_ok=True)\n"
            "with log_path.open(\"a\", encoding=\"utf-8\") as stream:\n"
            "    with redirect_stdout(stream), redirect_stderr(stream):\n"
            "        os.chdir(release)\n"
            "        sys.path.insert(0, str(release))\n"
            "        from chat_watchdog.fixed_action_timer import main\n"
            "        exit_code = main([\"--config\", str(config_path)])\n"
            "raise SystemExit(exit_code)\n"
        )

    def _ensure_launcher(self) -> None:
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        wanted = self._launcher_content()
        if self.launcher_path.exists():
            try:
                if self.launcher_path.read_text(encoding="utf-8") == wanted:
                    return
            except OSError:
                pass
        temp = self.launcher_path.with_suffix(self.launcher_path.suffix + ".tmp")
        temp.write_text(wanted, encoding="utf-8")
        temp.replace(self.launcher_path)

    def _task_command(self, target_url: str) -> str:
        conversation_id = conversation_id_from_url(target_url)
        return subprocess.list2cmdline([
            str(self.python_executable),
            str(self.launcher_path),
            conversation_id,
        ])

    def _run_schtasks(self, argv: list[str]):
        return self._runner(
            argv,
            capture_output=True,
            text=True,
            check=False,
            creationflags=_NO_WINDOW,
        )

    def ensure(self, target_url: str) -> None:
        conversation_id = conversation_id_from_url(target_url)
        config = self._load_or_default(target_url)
        config_path = self.config_path(target_url)
        fingerprint = (
            str(self.release_dir),
            str(self.python_executable),
            config.interval_minutes,
        )
        self._ensure_launcher()
        if config_path.exists() and self._applied.get(conversation_id) == fingerprint:
            self.legacy_launcher_path.unlink(missing_ok=True)
            return
        self._write_config(config)
        command = self._task_command(target_url)
        result = self._run_schtasks([
            "schtasks.exe",
            "/Create",
            "/F",
            "/SC",
            "MINUTE",
            "/MO",
            str(config.interval_minutes),
            "/TN",
            self.task_name(target_url),
            "/TR",
            command,
        ])
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "schtasks create failed").strip())
        self._applied[conversation_id] = fingerprint
        # Retire the old console launcher only after Task Scheduler has accepted
        # the new pythonw/.pyw action, so a failed migration cannot create a
        # mechanical-fallback coverage gap.
        self.legacy_launcher_path.unlink(missing_ok=True)

    def remove(self, target_url: str) -> None:
        conversation_id = conversation_id_from_url(target_url)
        self._delete_task(self.task_name(target_url), missing_ok=True)
        self._applied.pop(conversation_id, None)

    def _delete_task(self, task_name: str, *, missing_ok: bool) -> None:
        result = self._run_schtasks(
            ["schtasks.exe", "/Delete", "/F", "/TN", task_name]
        )
        if result.returncode != 0 and not missing_ok:
            raise RuntimeError((result.stderr or result.stdout or "schtasks delete failed").strip())

    def _task_names(self) -> set[str]:
        result = self._run_schtasks(
            ["schtasks.exe", "/Query", "/FO", "CSV", "/NH"]
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "schtasks query failed").strip())
        names: set[str] = set()
        for row in csv.reader((result.stdout or "").splitlines()):
            if not row:
                continue
            name = row[0].lstrip("\\")
            if name:
                names.add(name)
        return names

    def reconcile(self, registrations: Iterable[object]) -> None:
        desired_urls = {
            str(getattr(item, "target_url"))
            for item in registrations
            if isinstance(getattr(item, "target_url", None), str)
        }
        desired_names = {self.task_name(url) for url in desired_urls}
        desired_ids = {conversation_id_from_url(url) for url in desired_urls}
        self._applied = {
            conversation_id: fingerprint
            for conversation_id, fingerprint in self._applied.items()
            if conversation_id in desired_ids
        }

        # Establish every desired canonical task before retiring stale/legacy
        # tasks, so reconciliation never creates a fallback coverage gap.
        for target_url in sorted(desired_urls):
            self.ensure(target_url)

        for name in sorted(self._task_names()):
            if name.startswith(LEGACY_V2_PREFIX):
                self._delete_task(name, missing_ok=False)
                continue
            if name.startswith(TASK_PREFIX) and name not in desired_names:
                self._delete_task(name, missing_ok=False)
