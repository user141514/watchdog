from __future__ import annotations

import unittest

from chat_watchdog.observation_client import SidecarObservationPage
from chat_watchdog.simple_watchdog import SimpleWatcher


class MechanicalFallbackSeparationTests(unittest.TestCase):
    def test_normal_watcher_has_no_mechanical_timer_scheduler(self):
        self.assertFalse(hasattr(SimpleWatcher, "_fixed_prompt_due"))
        self.assertFalse(hasattr(SimpleWatcher, "_arm_fixed_prompt"))
        self.assertFalse(hasattr(SimpleWatcher, "_send_fixed_prompt"))

    def test_sidecar_page_has_no_mechanical_fixed_prompt_path(self):
        self.assertFalse(hasattr(SidecarObservationPage, "send_fixed_prompt"))


if __name__ == "__main__":
    unittest.main()
