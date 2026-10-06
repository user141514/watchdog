from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from threading import Event, Thread
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
        python.with_name("pythonw.exe").write_text("", encoding="utf-8")
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
            launcher = root / "fixed-action-current.pyw"
            self.assertTrue(launcher.is_file())
            self.assertIn(str(root / "venv" / "Scripts" / "pythonw.exe"), command)
            self.assertIn(str(launcher), command)
            self.assertIn(CID, command)
            self.assertNotIn("cmd.exe", command.lower())
            self.assertLessEqual(len(command), 261)
            launcher_text = launcher.read_text(encoding="utf-8")
            self.assertIn(repr(str(root / "releases" / "abc123")), launcher_text)
            compile(launcher_text, str(launcher), "exec")
            self.assertIn("from chat_watchdog.fixed_action_timer import main", launcher_text)
            self.assertIn('f"{conversation_id}.json"', launcher_text)
            self.assertNotIn("Sidecar", launcher_text)
            self.assertNotIn("fixed-action-v2", launcher_text)

    def test_manual_attachment_forces_task_scheduler_to_accept_current_projection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runner = Runner(query_stdout="")
            scheduler = self.scheduler(root, runner)

            scheduler.ensure(TARGET)
            scheduler.ensure_attached(TARGET)

            creates = [call for call, _ in runner.calls if "/Create" in call]
            queries = [call for call, _ in runner.calls if "/Query" in call]
            self.assertEqual(len(queries), 0)
            self.assertEqual(len(creates), 2)

    def test_manual_attachment_rewrites_existing_task_to_current_canonical_action(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runner = Runner(query_stdout=f'"\\ChatGPT Fixed ACTION - {CID}","N/A","Ready"')
            scheduler = self.scheduler(root, runner)

            scheduler.ensure(TARGET)
            scheduler.ensure_attached(TARGET)

            creates = [call for call, _ in runner.calls if "/Create" in call]
            self.assertEqual(len(creates), 2)

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

    def test_existing_builtin_prompt_is_upgraded_but_interval_is_preserved(self):
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
                    "prompt": "这是 15 分钟独立固定兜底。旧版本",
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            runner = Runner()
            scheduler = self.scheduler(root, runner)

            scheduler.ensure(TARGET)

            config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(config["prompt"], FIXED_ACTION_PROMPT)
            self.assertEqual(config["interval_minutes"], 7)
            create = next(call for call, _ in runner.calls if "/Create" in call)
            self.assertEqual(create[create.index("/MO") + 1], "7")

    def test_existing_custom_prompt_and_interval_are_preserved(self):
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

    def test_all_schtasks_calls_suppress_console_windows(self):
        with tempfile.TemporaryDirectory() as td:
            runner = Runner()
            scheduler = self.scheduler(Path(td), runner)

            scheduler.reconcile([SimpleNamespace(target_url=TARGET)])
            scheduler.remove(TARGET)

            expected = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            schtasks_calls = [
                kwargs
                for argv, kwargs in runner.calls
                if argv and argv[0].lower() == "schtasks.exe"
            ]
            self.assertTrue(schtasks_calls)
            self.assertTrue(all(kwargs.get("creationflags") == expected for kwargs in schtasks_calls))

    def test_legacy_cmd_launcher_is_retired_when_pythonw_launcher_is_written(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            legacy = root / "fixed-action-current.cmd"
            legacy.write_text("@echo off\n", encoding="utf-8")
            runner = Runner()
            scheduler = self.scheduler(root, runner)

            scheduler.ensure(TARGET)

            self.assertFalse(legacy.exists())
            self.assertTrue((root / "fixed-action-current.pyw").is_file())

    def test_concurrent_ensure_serializes_task_projection(self):
        class BlockingRunner(Runner):
            def __init__(self):
                super().__init__()
                self.entered = Event()
                self.release = Event()
                self.create_calls = 0

            def __call__(self, argv, **kwargs):
                self.calls.append((list(argv), dict(kwargs)))
                if "/Create" in argv:
                    self.create_calls += 1
                    self.entered.set()
                    self.release.wait(2)
                if "/Query" in argv:
                    return SimpleNamespace(returncode=0, stdout=self.query_stdout, stderr="")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as td:
            runner = BlockingRunner()
            scheduler = self.scheduler(Path(td), runner)
            first = Thread(target=scheduler.ensure, args=(TARGET,))
            second = Thread(target=scheduler.ensure, args=(TARGET,))
            first.start()
            self.assertTrue(runner.entered.wait(1))
            second.start()
            self.assertEqual(runner.create_calls, 1)
            runner.release.set()
            first.join(2)
            second.join(2)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(runner.create_calls, 1)

    def test_failed_task_replacement_keeps_legacy_launcher_for_coverage(self):
        class FailingCreateRunner(Runner):
            def __call__(self, argv, **kwargs):
                self.calls.append((list(argv), dict(kwargs)))
                if "/Create" in argv:
                    return SimpleNamespace(returncode=1, stdout="", stderr="create failed")
                if "/Query" in argv:
                    return SimpleNamespace(returncode=0, stdout=self.query_stdout, stderr="")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            legacy = root / "fixed-action-current.cmd"
            legacy.write_text("@echo off\n", encoding="utf-8")
            scheduler = self.scheduler(root, FailingCreateRunner())

            with self.assertRaisesRegex(RuntimeError, "create failed"):
                scheduler.ensure(TARGET)

            self.assertTrue(legacy.exists())
            self.assertTrue((root / "fixed-action-current.pyw").is_file())


if __name__ == "__main__":
    unittest.main()
