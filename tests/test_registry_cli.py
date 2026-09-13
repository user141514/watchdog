from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from chat_watchdog.cli import build_parser as build_legacy_parser
from chat_watchdog.registry_cli import build_parser, main


class RegistryCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = str(Path(self.temp.name) / "registry.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args: str) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--store", self.store, *args])
        return code, output.getvalue().strip()

    def test_crud_commands_persist_machine_readable_state(self):
        code, text = self.run_cli(
            "add",
            "agent-main",
            "https://chatgpt.com/g/g-p-agent/c/abc-123",
        )
        self.assertEqual(code, 0)
        self.assertIn("agent-main", text)

        code, text = self.run_cli("list", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(text)
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["watch_id"], "agent-main")
        self.assertEqual(payload[0]["state"], "active")

        self.assertEqual(self.run_cli("pause", "agent-main")[0], 0)
        payload = json.loads(self.run_cli("list", "--json")[1])
        self.assertEqual(payload[0]["state"], "paused")

        self.assertEqual(self.run_cli("arm", "agent-main")[0], 0)
        payload = json.loads(self.run_cli("list", "--json")[1])
        self.assertEqual(payload[0]["state"], "active")

        self.assertEqual(self.run_cli("remove", "agent-main")[0], 0)
        self.assertEqual(json.loads(self.run_cli("list", "--json")[1]), [])

    def test_add_accepts_optional_reanchor_binding(self):
        self.assertEqual(
            self.run_cli(
                "add",
                "anchored",
                "https://chatgpt.com/c/anchored",
                "--reanchor-scope",
                "scope-a",
                "--reanchor-epoch",
                "epoch-a",
            )[0],
            0,
        )
        payload = json.loads(self.run_cli("list", "--json")[1])
        self.assertEqual(payload[0]["reanchor_scope"], "scope-a")
        self.assertEqual(payload[0]["reanchor_epoch"], "epoch-a")

    def test_run_parser_exposes_registry_daemon_settings(self):
        args = build_parser().parse_args(
            [
                "--store",
                self.store,
                "run",
                "--relay-url",
                "http://127.0.0.1:9224",
                "--poll-seconds",
                "15",
                "--agent",
                "omp -p",
                "--reanchor-store",
                "C:/mem/reanchor",
                "--reanchor-cli",
                "C:/repo/reanchor/bin/reanchor.mjs",
            ]
        )
        self.assertEqual(args.command, "run")
        self.assertEqual(args.poll_seconds, 15.0)
        self.assertEqual(args.relay_url, "http://127.0.0.1:9224")
        self.assertEqual(args.reanchor_store, "C:/mem/reanchor")

    def test_legacy_single_conversation_parser_remains_available(self):
        args = build_legacy_parser().parse_args(
            ["--match-url", "/c/legacy", "--poll-seconds", "30"]
        )
        self.assertEqual(args.match_url, "/c/legacy")
        self.assertEqual(args.poll_seconds, 30.0)


if __name__ == "__main__":
    unittest.main()
