from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from chat_watchdog.cli import build_parser as build_watchdog_parser
from chat_watchdog.registry_cli import build_parser, main


CHAT_ID = "6aa542fd-708c-83ea-869a-721efd83d7f3"
CHAT_URL = f"https://chatgpt.com/g/g-p-example-agent/c/{CHAT_ID}"


class RegistryCliTests(unittest.TestCase):
    def test_parser_is_only_a_thin_client(self) -> None:
        args = build_parser().parse_args(["add", CHAT_URL])
        self.assertEqual(args.command, "add")
        self.assertEqual(args.url, CHAT_URL)
        self.assertEqual(args.control_url, "http://127.0.0.1:9235")

    def test_add_forwards_exact_url_to_running_registry(self) -> None:
        output = io.StringIO()
        with patch(
            "chat_watchdog.registry_cli._request",
            return_value={"conversation_id": CHAT_ID, "created": True},
        ) as request, redirect_stdout(output):
            code = main(["add", CHAT_URL])

        self.assertEqual(code, 0)
        request.assert_called_once_with(
            "http://127.0.0.1:9235",
            "POST",
            "/register",
            {"url": CHAT_URL},
        )
        self.assertEqual(output.getvalue().strip(), f"{CHAT_ID}\tcreated")

    def test_list_prints_machine_readable_registry_state(self) -> None:
        payload = {"watches": [{"conversation_id": CHAT_ID, "target_url": CHAT_URL}]}
        output = io.StringIO()
        with patch("chat_watchdog.registry_cli._request", return_value=payload), redirect_stdout(output):
            code = main(["list", "--json"])

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), payload["watches"])

    def test_main_watchdog_parser_exposes_registry_mode_without_removing_single_watch_mode(self) -> None:
        dynamic = build_watchdog_parser().parse_args(["--registry-port", "9235"])
        self.assertEqual(dynamic.registry_port, 9235)

        legacy = build_watchdog_parser().parse_args(
            ["--match-url", f"/c/{CHAT_ID}", "--poll-seconds", "30"]
        )
        self.assertEqual(legacy.match_url, f"/c/{CHAT_ID}")
        self.assertEqual(legacy.poll_seconds, 30.0)


if __name__ == "__main__":
    unittest.main()
