from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from chat_watchdog.fixed_action_scheduler import WindowsFixedActionScheduler
from chat_watchdog.fixed_action_timer import FIXED_ACTION_PROMPT


CID = "6ac34d31-2cc8-83ec-a770-430855a5d07d"
TARGET = f"https://chatgpt.com/c/{CID}"


class Runner:
    def __init__(self, query_stdout=""):
        self.calls = []
        self.query_stdout = query_stdout

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        if "/Query" in argv:
            return SimpleNamespace(returncode=0, stdout=self.query_stdout, stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


class FixedActionSchedulerTests(unittest.TestCase):
    def scheduler(self, root: Path, runner: Runner):
        release = root / "releases" / "abc123"
        release.mkdir(parents=True)
        python = root / "venv" / "Scripts" / "python.exe"
        python.parent.mkdir(parents=True)
        python.write_text("", encoding="utf-8")
        return WindowsFixedActionScheduler(
            runtime_root=root,
            release_dir=release,
            python_executable=python,
            runner=runner,
        )

    def test_ensure_creates_canonical_task_and_default_config(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runner = Runner()
            scheduler = self.scheduler(root, runner)

            scheduler.ensure(TARGET)

            config_path = root / "fixed-action-timer" / f"{CID}.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(config["target_url"], TARGET)
            self.assertEqual(config["interval_minutes"], 15)
            self.assertEqual(config["prompt"], FIXED_ACTION_PROMPT)

            create = next(call for call, _ in runner.calls if "/Create" in call)
            self.assertIn(f"ChatGPT Fixed ACTION - {CID}", create)
            command = create[create.index("/TR") + 1]
            launcher = root / "fixed-action-current.cmd"
            self.assertTrue(launcher.is_file())
            self.assertIn(str(launcher), command)
            self.assertIn(CID, command)
            self.assertLessEqual(len(command), 261)
            launcher_text = launcher.read_text(encoding="utf-8")
            self.assertIn(str(root / "releases" / "abc123"), launcher_text)
            self.assertIn("-m chat_watchdog fixed-action --config", launcher_text)
            self.assertIn("%CID%.json", launcher_text)
            self.assertNotIn("Sidecar", launcher_text)
            self.assertNotIn("fixed-action-v2", launcher_text)

    def test_reconcile_does_not_recreate_unchanged_task_every_poll(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runner = Runner()
            scheduler = self.scheduler(root, runner)
            registrations = [SimpleNamespace(target_url=TARGET)]

            scheduler.reconcile(registrations)
            scheduler.reconcile(registrations)

            creates = [call for call, _ in runner.calls if "/Create" in call]
            self.assertEqual(len(creates), 1)

    def test_existing_prompt_and_interval_are_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config_dir = root / "fixed-action-timer"
            config_dir.mkdir(parents=True)
            config_path = config_dir / f"{CID}.json"
            config_path.write_text(
                json.dumps({
                    "target_url": TARGET,
                    "relay_url": "http://127.0.0.1:9224",
                    "acceptance_timeout_seconds": 12,
                    "interval_minutes": 7,
                    "prompt": "custom fallback",
                }),
                encoding="utf-8",
            )
            runner = Runner()
            scheduler = self.scheduler(root, runner)

            scheduler.ensure(TARGET)

            config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(config["prompt"], "custom fallback")
            self.assertEqual(config["interval_minutes"], 7)
            create = next(call for call, _ in runner.calls if "/Create" in call)
            self.assertEqual(create[create.index("/MO") + 1], "7")

    def test_reconcile_deletes_v2_and_stale_legacy_tasks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            query = "\n".join([
                f'"\\ChatGPT Fixed ACTION - {CID}","N/A","Ready"',
                '"\\ChatGPT Fixed ACTION - deadbeef","N/A","Ready"',
                '"\\ChatGPT Fixed ACTION V2 - 6ac34d31","N/A","Ready"',
            ])
            runner = Runner(query_stdout=query)
            scheduler = self.scheduler(root, runner)

            scheduler.reconcile([SimpleNamespace(target_url=TARGET)])

            deletes = [call for call, _ in runner.calls if "/Delete" in call]
            deleted_names = {call[call.index("/TN") + 1] for call in deletes}
            self.assertIn("ChatGPT Fixed ACTION - deadbeef", deleted_names)
            self.assertIn("ChatGPT Fixed ACTION V2 - 6ac34d31", deleted_names)
            self.assertNotIn(f"ChatGPT Fixed ACTION - {CID}", deleted_names)

    def test_remove_deletes_only_exact_canonical_task(self):
        with tempfile.TemporaryDirectory() as td:
            runner = Runner()
            scheduler = self.scheduler(Path(td), runner)
            scheduler.remove(TARGET)
            delete = next(call for call, _ in runner.calls if "/Delete" in call)
            self.assertEqual(delete[delete.index("/TN") + 1], f"ChatGPT Fixed ACTION - {CID}")


if __name__ == "__main__":
    unittest.main()
