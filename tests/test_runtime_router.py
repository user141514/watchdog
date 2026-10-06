from __future__ import annotations

import subprocess
import sys
import unittest

from chat_watchdog.cli import build_parser
from chat_watchdog.simple_watchdog import SimpleWatcher


class RuntimeRouterTests(unittest.TestCase):
    def test_fixed_action_subcommand_is_available_from_canonical_package_entry(self):
        result = subprocess.run(
            [sys.executable, "-m", "chat_watchdog", "fixed-action", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("--config", result.stdout)

    def test_normal_entry_remains_backward_compatible(self):
        result = subprocess.run(
            [sys.executable, "-m", "chat_watchdog", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("--simple", result.stdout)

    def test_fixed_action_scheduler_requires_explicit_runtime_flag(self):
        parser = build_parser()
        default = parser.parse_args(["--simple", "--registry-port", "9235"])
        enabled = parser.parse_args([
            "--simple", "--registry-port", "9235", "--fixed-action-scheduler"
        ])
        self.assertFalse(default.fixed_action_scheduler)
        self.assertTrue(enabled.fixed_action_scheduler)

    def test_normal_simple_watcher_does_not_own_mechanical_timer(self):
        self.assertFalse(hasattr(SimpleWatcher, "_fixed_prompt_due"))
        self.assertFalse(hasattr(SimpleWatcher, "_arm_fixed_prompt"))
        self.assertFalse(hasattr(SimpleWatcher, "_send_fixed_prompt"))


if __name__ == "__main__":
    unittest.main()
