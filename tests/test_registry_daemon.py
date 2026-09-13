from __future__ import annotations

from dataclasses import dataclass
import unittest

from chat_watchdog.registry import WatchRegistry
from chat_watchdog.registry_daemon import WatchRegistry as CompatibilityWatchRegistry


CHAT_A = "6aa542fd-708c-83ea-869a-721efd83d7f3"
CHAT_B = "6aa525c0-e748-83ea-a617-df96d68d63ec"


@dataclass
class FakeWatcher:
    should_stop: bool = False
    steps: int = 0
    closed: bool = False
    completion_text: str | None = None

    def step(self) -> None:
        self.steps += 1

    def close(self) -> None:
        self.closed = True


class DynamicWatchTests(unittest.TestCase):
    def test_compatibility_module_has_no_second_registry_implementation(self) -> None:
        self.assertIs(CompatibilityWatchRegistry, WatchRegistry)

    def test_new_registration_is_visible_immediately_and_completed_watch_does_not_remove_sibling(self) -> None:
        watchers: dict[str, FakeWatcher] = {}

        def factory(url: str) -> FakeWatcher:
            watcher = FakeWatcher()
            watchers[url] = watcher
            return watcher

        registry = WatchRegistry(factory)
        url_a = f"https://chatgpt.com/c/{CHAT_A}"
        url_b = f"https://chatgpt.com/c/{CHAT_B}"

        registry.register(url_a)
        registry.step_all()
        self.assertEqual(registry.list_ids(), [CHAT_A])

        registry.register(url_b)
        watchers[url_a].should_stop = True
        registry.step_all()

        self.assertTrue(watchers[url_a].closed)
        self.assertFalse(watchers[url_b].closed)
        self.assertEqual(registry.list_ids(), [CHAT_B])
        self.assertEqual(watchers[url_b].steps, 1)

        registry.close()
        self.assertTrue(watchers[url_b].closed)


if __name__ == "__main__":
    unittest.main()
