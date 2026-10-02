import unittest

from chat_watchdog.cli import _SupervisorWatcher
from chat_watchdog.observation_client import SidecarObservationPage
from chat_watchdog.supervisor import Supervisor
from test_sidecar_simple_watchdog import Owner, REGISTRATION, TARGET, sample, watcher


class WatchdogBindingSignalTests(unittest.TestCase):
    def test_only_missing_current_owner_binding_requests_registry_rebind(self):
        for reason, expected in (
            ("watchdog_binding_required", True),
            ("watchdog_registration_revoked", False),
            ("stale_state", False),
            ("delivery_uncertain", False),
        ):
            with self.subTest(reason=reason):
                owner = Owner(receipt={"accepted": False, "reason": reason})
                runtime = watcher(owner)
                runtime.step()
                self.assertEqual(runtime.requires_owner_rebind, expected)

                classic_owner = Owner(receipt={"accepted": False, "reason": reason})
                observation, intent = classic_owner.clients()
                page = SidecarObservationPage(TARGET, observation, intent, registration_id=REGISTRATION)
                classic = _SupervisorWatcher(page, Supervisor(page, None, intent_client=page, state_client=page))
                classic.step()
                self.assertEqual(classic.requires_owner_rebind, expected)

    def test_known_missing_binding_does_not_consume_unused_stop_or_refresh_fence(self):
        for fields in (
            {"generating": True, "terminal": False, "body": "incomplete"},
            {"generating": False, "terminal": False, "body": "incomplete"},
        ):
            with self.subTest(fields=fields):
                clock = [0.0]
                owner = Owner([sample(**fields)],
                              receipt={"accepted": False, "reason": "watchdog_binding_required"})
                runtime = watcher(owner, clock=lambda: clock[0])
                runtime.step()
                clock[0] = 900
                runtime.step()
                self.assertTrue(runtime.requires_owner_rebind)
                self.assertIsNone(runtime.durable_state["liveness_attempted_for"])


if __name__ == "__main__":
    unittest.main()
