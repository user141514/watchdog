from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from chat_watchdog.registry import WatchRegistry, canonical_conversation_url


class ConversationUrlTests(unittest.TestCase):
    def test_accepts_direct_and_project_conversation_urls(self):
        self.assertEqual(
            canonical_conversation_url("https://chatgpt.com/c/abc-123"),
            "https://chatgpt.com/c/abc-123",
        )
        self.assertEqual(
            canonical_conversation_url(
                "https://chatgpt.com/g/g-p-example-agent/c/def-456"
            ),
            "https://chatgpt.com/g/g-p-example-agent/c/def-456",
        )

    def test_rejects_non_conversation_or_ambiguous_urls(self):
        invalid = [
            "http://chatgpt.com/c/abc",
            "https://example.com/c/abc",
            "https://chatgpt.com/",
            "https://chatgpt.com/g/g-p-example/project",
            "https://chatgpt.com/c/abc/extra",
            "https://chatgpt.com/c/abc?foo=bar",
            "https://chatgpt.com/c/abc#fragment",
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                canonical_conversation_url(value)


class WatchRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "registry.sqlite3"
        self.registry = WatchRegistry(self.db)

    def tearDown(self):
        self.registry.close()
        self.temp.cleanup()

    def test_add_survives_reopen_and_defaults_active(self):
        created = self.registry.add(
            "agent-main",
            "https://chatgpt.com/g/g-p-agent/c/abc-123",
        )
        self.assertEqual(created.watch_id, "agent-main")
        self.assertEqual(created.state, "active")
        self.assertIsNone(created.last_result)

        self.registry.close()
        self.registry = WatchRegistry(self.db)
        loaded = self.registry.get("agent-main")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.target_url, created.target_url)
        self.assertEqual(loaded.created_at, created.created_at)

    def test_duplicate_target_and_invalid_watch_id_are_rejected(self):
        url = "https://chatgpt.com/c/abc-123"
        self.registry.add("one", url)
        with self.assertRaises(ValueError):
            self.registry.add("two", url)
        for bad_id in ["", "has space", "/path", "x" * 65]:
            with self.subTest(bad_id=bad_id), self.assertRaises(ValueError):
                self.registry.add(bad_id, f"https://chatgpt.com/c/{len(bad_id)}")

    def test_reanchor_scope_and_epoch_must_be_paired(self):
        with self.assertRaises(ValueError):
            self.registry.add(
                "scope-only",
                "https://chatgpt.com/c/scope-only",
                reanchor_scope="scope-a",
            )
        with self.assertRaises(ValueError):
            self.registry.add(
                "epoch-only",
                "https://chatgpt.com/c/epoch-only",
                reanchor_epoch="epoch-a",
            )

        created = self.registry.add(
            "paired",
            "https://chatgpt.com/c/paired",
            reanchor_scope="scope-a",
            reanchor_epoch="epoch-a",
        )
        self.assertEqual(created.reanchor_scope, "scope-a")
        self.assertEqual(created.reanchor_epoch, "epoch-a")

    def test_state_and_result_updates_are_durable(self):
        self.registry.add("stateful", "https://chatgpt.com/c/stateful")
        paused = self.registry.set_state("stateful", "paused")
        self.assertEqual(paused.state, "paused")
        recorded = self.registry.record_result("stateful", "need_input")
        self.assertEqual(recorded.last_result, "need_input")
        completed = self.registry.set_state("stateful", "completed")
        self.assertEqual(completed.state, "completed")

        self.registry.close()
        self.registry = WatchRegistry(self.db)
        loaded = self.registry.get("stateful")
        self.assertEqual(loaded.state, "completed")
        self.assertEqual(loaded.last_result, "need_input")

        with self.assertRaises(ValueError):
            self.registry.set_state("stateful", "unknown")
        with self.assertRaises(KeyError):
            self.registry.set_state("missing", "active")

    def test_list_filters_by_state_and_remove_is_idempotent(self):
        self.registry.add("active", "https://chatgpt.com/c/active")
        self.registry.add("paused", "https://chatgpt.com/c/paused")
        self.registry.set_state("paused", "paused")

        self.assertEqual(
            [entry.watch_id for entry in self.registry.list(state="active")],
            ["active"],
        )
        self.assertEqual(
            {entry.watch_id for entry in self.registry.list()},
            {"active", "paused"},
        )
        self.assertTrue(self.registry.remove("active"))
        self.assertFalse(self.registry.remove("active"))
        self.assertIsNone(self.registry.get("active"))


if __name__ == "__main__":
    unittest.main()
