from __future__ import annotations

from dataclasses import dataclass
import unittest

from chat_watchdog.registry import WatchRegistry, conversation_id_from_url


CHAT_ID = "6aa542fd-708c-83ea-869a-721efd83d7f3"
PROJECT_URL = f"https://chatgpt.com/g/g-p-example-agent/c/{CHAT_ID}"
ROOT_URL = f"https://chatgpt.com/c/{CHAT_ID}"


@dataclass
class FakeWatcher:
    url: str
    should_stop: bool = False
    steps: int = 0
    closed: bool = False
    completion_text: str = "final watchdog result"
    state: str = "active"

    def step(self) -> None:
        self.steps += 1

    def close(self) -> None:
        self.closed = True


class ConversationIdentityTests(unittest.TestCase):
    def test_uses_chatgpt_conversation_uuid(self) -> None:
        self.assertEqual(conversation_id_from_url(PROJECT_URL), CHAT_ID)
        self.assertEqual(conversation_id_from_url(ROOT_URL), CHAT_ID)

    def test_rejects_non_conversation_urls(self) -> None:
        invalid = [
            "https://example.com/c/6aa542fd-708c-83ea-869a-721efd83d7f3",
            "https://chatgpt.com/g/g-p-example/project",
            "https://chatgpt.com/c/not-a-uuid",
        ]
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(ValueError):
                conversation_id_from_url(url)


class WatchRegistryTests(unittest.TestCase):
    def test_deduplicates_same_conversation_across_url_shapes(self) -> None:
        created: list[FakeWatcher] = []

        def factory(url: str) -> FakeWatcher:
            watcher = FakeWatcher(url)
            created.append(watcher)
            return watcher

        registry = WatchRegistry(factory)

        first = registry.register(PROJECT_URL)
        second = registry.register(ROOT_URL)

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.conversation_id, CHAT_ID)
        self.assertEqual(second.conversation_id, CHAT_ID)
        self.assertEqual(len(created), 1)
        self.assertEqual(registry.list_ids(), [CHAT_ID])
        self.assertEqual(registry.list()[0].state, "active")

    def test_unregister_closes_watcher(self) -> None:
        watcher = FakeWatcher(PROJECT_URL)
        registry = WatchRegistry(lambda _url: watcher)
        registry.register(PROJECT_URL)

        self.assertTrue(registry.unregister(CHAT_ID))
        self.assertTrue(watcher.closed)
        self.assertEqual(registry.list_ids(), [])
        self.assertFalse(registry.unregister(CHAT_ID))

    def test_step_removes_completed_watcher(self) -> None:
        watcher = FakeWatcher(PROJECT_URL)
        registry = WatchRegistry(lambda _url: watcher)
        registry.register(PROJECT_URL)

        registry.step_all()
        self.assertEqual(watcher.steps, 1)
        self.assertEqual(registry.list_ids(), [CHAT_ID])

        watcher.state = "need_input"
        registry.step_all()
        self.assertEqual(registry.list()[0].state, "need_input")
        self.assertIsNone(registry.completion(CHAT_ID))

        watcher.should_stop = True
        registry.step_all()

        self.assertEqual(watcher.steps, 3)
        self.assertTrue(watcher.closed)
        self.assertEqual(registry.list_ids(), [])
        completion = registry.completion(CHAT_ID)
        self.assertIsNotNone(completion)
        self.assertEqual(completion.result, "final watchdog result")
        self.assertTrue(registry.ack_completion(CHAT_ID))
        self.assertIsNone(registry.completion(CHAT_ID))


if __name__ == "__main__":
    unittest.main()
