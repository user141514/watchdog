from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tempfile
from threading import Event, Thread
import time
import unittest

from chat_watchdog.registry import WatchRegistry, conversation_id_from_url


CHAT_ID = "6aa542fd-708c-83ea-869a-721efd83d7f3"
CHAT_ID_2 = "7bb542fd-708c-83ea-869a-721efd83d7f4"
PROJECT_URL = f"https://chatgpt.com/g/g-p-example-agent/c/{CHAT_ID}"
ROOT_URL = f"https://chatgpt.com/c/{CHAT_ID}"
ROOT_URL_2 = f"https://chatgpt.com/c/{CHAT_ID_2}"


@dataclass
class FakeWatcher:
    url: str
    should_stop: bool = False
    steps: int = 0
    closed: bool = False
    completion_text: str = "final watchdog result"
    state: str = "active"
    continuation_prompt: str | None = None

    def set_continuation_prompt(self, prompt: str) -> None:
        self.continuation_prompt = prompt

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


class FakeProgressStore:
    def __init__(self) -> None:
        self.ensured = []
        self.suspended = []
        self.removed = []

    def ensure(self, conversation_id: str, target_url: str):
        self.ensured.append((conversation_id, target_url))

    def suspend(self, conversation_id: str):
        self.suspended.append(conversation_id)

    def remove(self, conversation_id: str):
        self.removed.append(conversation_id)


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
        self.assertEqual(first.task_id, second.task_id)
        self.assertEqual(len(created), 1)
        self.assertEqual(registry.list_ids(), [CHAT_ID])
        self.assertEqual(registry.list()[0].task_id, first.task_id)
        self.assertEqual(registry.list()[0].state, "active")

    def test_register_rechecks_task_identity_after_preflight_race(self) -> None:
        registry: WatchRegistry | None = None
        raced = False

        def preflight(_url: str) -> None:
            nonlocal raced
            if raced:
                return
            raced = True
            assert registry is not None
            registry.register(PROJECT_URL, task_id="task-winner")

        registry = WatchRegistry(
            lambda url: FakeWatcher(url),
            registration_preflight=preflight,
        )

        with self.assertRaisesRegex(RuntimeError, "different task"):
            registry.register(PROJECT_URL, task_id="task-loser")

        self.assertEqual(registry.list()[0].task_id, "task-winner")
        registry.close()

    def test_task_prompt_is_versioned_injected_and_survives_rebind(self) -> None:
        watcher = FakeWatcher(PROJECT_URL)
        registry = WatchRegistry(lambda _url: watcher)
        registered = registry.register(PROJECT_URL, task_id="task-alpha")

        initial = registry.prompt(registered.task_id)
        self.assertEqual(initial.version, 0)
        self.assertEqual(initial.step_index, 0)
        self.assertIsNone(initial.step_prompt)

        updated = registry.update_prompt(
            registered.task_id,
            expected_version=0,
            step_index=1,
            step_prompt="只推进当前最小可验证步骤。",
            updated_by="codex",
        )
        self.assertEqual(updated.version, 1)
        self.assertEqual(updated.step_index, 1)
        self.assertEqual(updated.updated_by, "codex")

        with self.assertRaisesRegex(RuntimeError, "prompt version conflict"):
            registry.update_prompt(
                registered.task_id,
                expected_version=0,
                step_index=2,
                step_prompt="这个更新已经过期。",
                updated_by="stale-agent",
            )

        registry.step_all()
        self.assertIsNotNone(watcher.continuation_prompt)
        self.assertIn("task_id=task-alpha", watcher.continuation_prompt)
        self.assertIn("prompt_version=1", watcher.continuation_prompt)
        self.assertIn("step_index=1", watcher.continuation_prompt)
        self.assertIn("只推进当前最小可验证步骤", watcher.continuation_prompt)
        self.assertIn("SUPERVISOR_DONE", watcher.continuation_prompt)
        self.assertIn("[SUPERVISOR_STATE: NEED_INPUT]", watcher.continuation_prompt)

        registry.rebind(registered.task_id, ROOT_URL_2)
        rebound = registry.prompt(registered.task_id)
        self.assertEqual(rebound.version, 1)
        self.assertEqual(rebound.step_index, 1)
        self.assertEqual(rebound.step_prompt, "只推进当前最小可验证步骤。")
        registry.close()

    def test_prompt_update_waits_for_inflight_send_generation(self) -> None:
        entered = Event()
        release = Event()
        update_done = Event()

        class BlockingWatcher(FakeWatcher):
            def __init__(self, url: str) -> None:
                super().__init__(url)
                self.sent_prompts: list[str | None] = []
                self._block_once = True

            def set_continuation_prompt(self, prompt: str) -> None:
                self.continuation_prompt = prompt
                if self._block_once:
                    self._block_once = False
                    entered.set()
                    release.wait(timeout=2)

            def step(self) -> None:
                self.steps += 1
                self.sent_prompts.append(self.continuation_prompt)

        watcher = BlockingWatcher(PROJECT_URL)
        registry = WatchRegistry(lambda _url: watcher)
        registered = registry.register(PROJECT_URL, task_id="task-race")
        registry.update_prompt(
            registered.task_id,
            expected_version=0,
            step_index=1,
            step_prompt="FIRST",
            updated_by="test",
        )

        step_thread = Thread(target=registry.step_all)
        step_thread.start()
        self.assertTrue(entered.wait(timeout=2))

        updated: list[object] = []

        def update() -> None:
            updated.append(
                registry.update_prompt(
                    registered.task_id,
                    expected_version=1,
                    step_index=2,
                    step_prompt="SECOND",
                    updated_by="test",
                )
            )
            update_done.set()

        update_thread = Thread(target=update)
        update_thread.start()
        time.sleep(0.05)
        self.assertFalse(
            update_done.is_set(),
            "prompt update must not return while an older prompt step is in-flight",
        )

        release.set()
        step_thread.join(timeout=2)
        update_thread.join(timeout=2)
        self.assertFalse(step_thread.is_alive())
        self.assertFalse(update_thread.is_alive())
        self.assertTrue(update_done.is_set())
        self.assertEqual(registry.prompt(registered.task_id).version, 2)
        self.assertIn("prompt_version=1", watcher.sent_prompts[0] or "")
        self.assertIn("FIRST", watcher.sent_prompts[0] or "")

        registry.step_all()
        self.assertEqual(len(watcher.sent_prompts), 2)
        self.assertIn("prompt_version=2", watcher.sent_prompts[1] or "")
        self.assertIn("SECOND", watcher.sent_prompts[1] or "")
        registry.close()

    def test_protocol_v4_fails_closed_for_watcher_without_prompt_injection(self) -> None:
        @dataclass
        class LegacyWatcher:
            should_stop: bool = False
            completion_text: str | None = None
            state: str = "waiting"
            steps: int = 0
            closed: bool = False

            def step(self) -> None:
                self.steps += 1

            def close(self) -> None:
                self.closed = True

        watcher = LegacyWatcher()
        registry = WatchRegistry(lambda _url: watcher)
        registry.register(PROJECT_URL, task_id="task-legacy")

        registry.step_all()

        self.assertEqual(watcher.steps, 0)
        registration = registry.list()[0]
        self.assertEqual(registration.state, "degraded")
        self.assertIn("does not support continuation prompt injection", registration.last_error or "")
        registry.close()

    def test_task_prompt_persists_across_registry_restart(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store_path = Path(root) / "registry.sqlite3"
            first = WatchRegistry(
                lambda url: FakeWatcher(url),
                store_path=store_path,
                connect_on_register=False,
            )
            registered = first.register(PROJECT_URL, task_id="task-persist")
            first.update_prompt(
                registered.task_id,
                expected_version=0,
                step_index=3,
                step_prompt="继续执行第三步。",
                updated_by="ui",
            )
            first.close()

            second = WatchRegistry(
                lambda url: FakeWatcher(url),
                store_path=store_path,
                connect_on_register=False,
            )
            restored = second.prompt("task-persist")
            self.assertEqual(restored.version, 1)
            self.assertEqual(restored.step_index, 3)
            self.assertEqual(restored.step_prompt, "继续执行第三步。")
            self.assertEqual(restored.updated_by, "ui")
            second.close()

    def test_rebind_preserves_task_identity_across_conversation_replacement(self) -> None:
        created: list[FakeWatcher] = []

        def factory(url: str) -> FakeWatcher:
            watcher = FakeWatcher(url)
            created.append(watcher)
            return watcher

        registry = WatchRegistry(factory)
        first = registry.register(PROJECT_URL, task_label="Research task")

        rebound = registry.rebind(first.task_id, ROOT_URL_2)

        self.assertTrue(rebound.changed)
        self.assertEqual(rebound.task_id, first.task_id)
        self.assertEqual(rebound.previous_conversation_id, CHAT_ID)
        self.assertEqual(rebound.conversation_id, CHAT_ID_2)
        self.assertTrue(created[0].closed)
        self.assertEqual(registry.list_ids(), [CHAT_ID_2])
        registration = registry.list()[0]
        self.assertEqual(registration.task_id, first.task_id)
        self.assertEqual(registration.task_label, "Research task")
        self.assertEqual(registration.target_url, ROOT_URL_2)

    def test_restart_suspends_persisted_progress_window(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store_path = Path(root) / "registry.sqlite3"
            first_progress = FakeProgressStore()
            first = WatchRegistry(
                lambda url: FakeWatcher(url),
                store_path=store_path,
                progress_store=first_progress,
            )
            first.register(PROJECT_URL)
            first.close()

            second_progress = FakeProgressStore()
            second = WatchRegistry(
                lambda url: FakeWatcher(url),
                store_path=store_path,
                progress_store=second_progress,
            )
            self.assertEqual(second_progress.ensured, [(CHAT_ID, PROJECT_URL)])
            self.assertEqual(second_progress.suspended, [CHAT_ID])
            second.close()

    def test_unregister_closes_watcher(self) -> None:
        watcher = FakeWatcher(PROJECT_URL)
        registry = WatchRegistry(lambda _url: watcher)
        registry.register(PROJECT_URL)

        self.assertTrue(registry.unregister(CHAT_ID))
        self.assertTrue(watcher.closed)
        self.assertEqual(registry.list_ids(), [])
        self.assertFalse(registry.unregister(CHAT_ID))

    def test_mymem_lite_follows_watch_registration_lifecycle(self) -> None:
        watcher = FakeWatcher(PROJECT_URL)
        progress = FakeProgressStore()
        registry = WatchRegistry(lambda _url: watcher, progress_store=progress)

        registry.register(PROJECT_URL)
        self.assertEqual(progress.ensured, [(CHAT_ID, PROJECT_URL)])
        self.assertEqual(progress.removed, [])

        self.assertTrue(registry.unregister(CHAT_ID))
        self.assertEqual(progress.removed, [CHAT_ID])

    def test_step_removes_completed_watcher(self) -> None:
        watcher = FakeWatcher(PROJECT_URL)
        progress = FakeProgressStore()
        registry = WatchRegistry(lambda _url: watcher, progress_store=progress)
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
        self.assertEqual(progress.removed, [CHAT_ID])
        completion = registry.completion(CHAT_ID)
        self.assertIsNotNone(completion)
        self.assertEqual(completion.result, "final watchdog result")
        self.assertTrue(registry.ack_completion(CHAT_ID))
        self.assertIsNone(registry.completion(CHAT_ID))


if __name__ == "__main__":
    unittest.main()
