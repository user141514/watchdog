from __future__ import annotations

import unittest

from chat_watchdog.send_admission import SidecarSendAdmission


class SidecarSendAdmissionTests(unittest.TestCase):
    def test_client_posts_watchdog_source_and_exact_target(self) -> None:
        calls = []

        def request_json(endpoint, payload):
            calls.append((endpoint, payload))
            return {"admitted": False, "retryAfterMs": 42000, "lastAdmittedAt": 7}

        client = SidecarSendAdmission(
            "http://127.0.0.1:7337/internal/send-admission",
            request_json=request_json,
        )
        result = client.admit("https://chatgpt.com/c/00000000-0000-0000-0000-000000000001")

        self.assertFalse(result.admitted)
        self.assertEqual(result.retry_after_ms, 42000)
        self.assertEqual(
            calls,
            [(
                "http://127.0.0.1:7337/internal/send-admission",
                {
                    "source": "watchdog",
                    "target": "https://chatgpt.com/c/00000000-0000-0000-0000-000000000001",
                },
            )],
        )

    def test_invalid_owner_response_fails_closed(self) -> None:
        client = SidecarSendAdmission(
            "http://127.0.0.1:7337/internal/send-admission",
            request_json=lambda _endpoint, _payload: {"status": "ok"},
        )
        with self.assertRaises(RuntimeError):
            client.admit("https://chatgpt.com/c/00000000-0000-0000-0000-000000000001")


if __name__ == "__main__":
    unittest.main()
