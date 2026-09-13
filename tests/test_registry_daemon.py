from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from chat_watchdog.registry import WatchRegistry
from chat_watchdog.registry_daemon import WatchDaemon
from chat_watchdog.supervisor import StepResult


class FakeSupervisor:
    def __init__(self, results=None, *, error: Exception | None = None):
        self.results = list(results or [StepResult.ACTIVE])
        self.error = error
        self.should_stop = False
        self.steps = 0

    def step(self):
        self.steps += 1
        if self.error is not None:
            raise self.error
        result = self.results.pop(0) if self.results else StepResult.ACTIVE
        if result is StepResult.DONE:
            self.should_stop = True
        return result


class FakeRuntime:
    def __init__(self, entry, supervisor=None):
        self.entry = entry
        self.supervisor = supervisor or FakeSupervisor()
        self.closed = False

    def close(self):
        self.closed = True


class RuntimeFactory:
    def __init__(self):
        self.created = []
        self.supervisors = {}

    def __call__(self, entry):
        runtime = FakeRuntime(entry, self.supervisors.get(entry.watch_id))
        self.created.append(runtime)
        return runtime


class WatchDaemonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.registry = WatchRegistry(Path(self.temp.name) / "registry.sqlite3")
        self.factory = RuntimeFactory()
        self.daemon = WatchDaemon(self.registry, self.factory)

    def tearDown(self):
        self.daemon.close()
        self.registry.close()
        self.temp.cleanup()

    def add(self, watch_id: str):
        return self.registry.add(watch_id, f"https://chatgpt.com/c/{watch_id}")

    def test_reconcile_starts_watch_added_after_daemon_creation(self):
        self.assertEqual(self.daemon.runtime_ids, ())
        self.add("later")
        self.daemon.reconcile()
        self.assertEqual(self.daemon.runtime_ids, ("later",))
        self.assertEqual([runtime.entry.watch_id for runtime in self.factory.created], ["later"])

    def test_pause_and_remove_close_only_affected_runtime(self):
        self.add("one")
        self.add("two")
        self.daemon.reconcile()
        one, two = self.factory.created

        self.registry.set_state("one", "paused")
        self.daemon.reconcile()
        self.assertTrue(one.closed)
        self.assertFalse(two.closed)
        self.assertEqual(self.daemon.runtime_ids, ("two",))

        self.registry.remove("two")
        self.daemon.reconcile()
        self.assertTrue(two.closed)
        self.assertEqual(self.daemon.runtime_ids, ())

    def test_done_is_persisted_and_not_recreated(self):
        self.add("done")
        supervisor = FakeSupervisor([StepResult.DONE])
        self.factory.supervisors["done"] = supervisor

        results = self.daemon.step()
        self.assertEqual(results, {"done": StepResult.DONE})
        entry = self.registry.get("done")
        self.assertEqual(entry.state, "completed")
        self.assertEqual(entry.last_result, "done")
        self.assertEqual(self.daemon.runtime_ids, ())
        self.assertTrue(self.factory.created[0].closed)

        self.daemon.reconcile()
        self.assertEqual(len(self.factory.created), 1)

    def test_completed_watch_can_be_explicitly_armed_again(self):
        self.add("rearm")
        self.factory.supervisors["rearm"] = FakeSupervisor([StepResult.DONE])
        self.daemon.step()
        self.registry.set_state("rearm", "active")
        self.factory.supervisors["rearm"] = FakeSupervisor([StepResult.ACTIVE])

        self.daemon.reconcile()
        self.assertEqual(self.daemon.runtime_ids, ("rearm",))
        self.assertEqual(len(self.factory.created), 2)

    def test_one_runtime_failure_does_not_block_sibling(self):
        self.add("broken")
        self.add("healthy")
        self.factory.supervisors["broken"] = FakeSupervisor(error=RuntimeError("boom"))
        healthy = FakeSupervisor([StepResult.ACTIVE])
        self.factory.supervisors["healthy"] = healthy

        results = self.daemon.step()
        self.assertNotIn("broken", results)
        self.assertEqual(results["healthy"], StepResult.ACTIVE)
        self.assertEqual(healthy.steps, 1)
        self.assertEqual(self.registry.get("healthy").last_result, "active")
        broken_runtime = next(
            runtime for runtime in self.factory.created if runtime.entry.watch_id == "broken"
        )
        self.assertTrue(broken_runtime.closed)

    def test_close_closes_all_runtimes(self):
        self.add("one")
        self.add("two")
        self.daemon.reconcile()
        runtimes = list(self.factory.created)
        self.daemon.close()
        self.assertTrue(all(runtime.closed for runtime in runtimes))
        self.assertEqual(self.daemon.runtime_ids, ())


if __name__ == "__main__":
    unittest.main()
