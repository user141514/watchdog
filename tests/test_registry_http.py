from __future__ import annotations

from dataclasses import dataclass
import json
from threading import Thread
import unittest
from urllib.request import Request, urlopen

from chat_watchdog.registry import WatchRegistry, create_control_server


CHAT_ID = "6aa542fd-708c-83ea-869a-721efd83d7f3"
CHAT_URL = f"https://chatgpt.com/g/g-p-example-agent/c/{CHAT_ID}"


@dataclass
class FakeWatcher:
    should_stop: bool = False
    closed: bool = False
    completion_text: str = "final watchdog result"
    state: str = "active"

    def step(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class RegistryHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.created: list[FakeWatcher] = []

        def factory(_url: str) -> FakeWatcher:
            watcher = FakeWatcher()
            self.created.append(watcher)
            return watcher

        self.registry = WatchRegistry(factory)
        self.server = create_control_server(self.registry, "127.0.0.1", 0)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        self.registry.close()

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={"content-type": "application/json"},
        )
        with urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_register_is_idempotent_and_list_uses_conversation_id(self) -> None:
        status, first = self.request("POST", "/register", {"url": CHAT_URL})
        self.assertEqual(status, 200)
        self.assertEqual(first, {"conversation_id": CHAT_ID, "created": True})

        _, second = self.request("POST", "/register", {"url": CHAT_URL})
        self.assertEqual(second, {"conversation_id": CHAT_ID, "created": False})
        self.assertEqual(len(self.created), 1)

        _, listing = self.request("GET", "/watches")
        self.assertEqual(
            listing,
            {"watches": [{"conversation_id": CHAT_ID, "target_url": CHAT_URL, "state": "active"}]},
        )

        self.created[0].state = "need_input"
        _, paused = self.request("GET", "/watches")
        self.assertEqual(paused["watches"][0]["state"], "need_input")

    def test_unregister_closes_registered_watcher(self) -> None:
        self.request("POST", "/register", {"url": CHAT_URL})
        _, result = self.request("POST", "/unregister", {"conversation_id": CHAT_ID})

        self.assertEqual(result, {"conversation_id": CHAT_ID, "removed": True})
        self.assertTrue(self.created[0].closed)
        _, listing = self.request("GET", "/watches")
        self.assertEqual(listing, {"watches": []})

    def test_completion_receipt_survives_active_removal_until_ack(self) -> None:
        self.request("POST", "/register", {"url": CHAT_URL})
        self.created[0].should_stop = True
        self.registry.step_all()

        _, completion = self.request("POST", "/completion", {"url": CHAT_URL})
        self.assertEqual(
            completion,
            {
                "conversation_id": CHAT_ID,
                "active": False,
                "completed": True,
                "result": "final watchdog result",
            },
        )

        _, ack = self.request("POST", "/completion/ack", {"url": CHAT_URL})
        self.assertEqual(ack, {"conversation_id": CHAT_ID, "removed": True})
        _, missing = self.request("POST", "/completion", {"url": CHAT_URL})
        self.assertEqual(
            missing,
            {
                "conversation_id": CHAT_ID,
                "active": False,
                "completed": False,
                "result": None,
            },
        )


if __name__ == "__main__":
    unittest.main()
