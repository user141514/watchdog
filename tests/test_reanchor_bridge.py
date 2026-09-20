from __future__ import annotations

import unittest

from chat_watchdog.model import PageSnapshot, Phase
from chat_watchdog.reanchor_bridge import ReanchorBridge


class FakeCli:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def call(self, command: str, payload: dict) -> dict:
        self.calls.append((command, payload))
        return {"accepted": True}


class ReanchorBridgeIdentityTests(unittest.TestCase):
    def bridge(self) -> tuple[ReanchorBridge, FakeCli]:
        cli = FakeCli()
        return ReanchorBridge(cli, "scope", "owner", {}), cli

    def test_pending_reply_accepts_stable_ids_despite_virtualized_counts(self) -> None:
        bridge, cli = self.bridge()
        snapshot = PageSnapshot(
            phase=Phase.FINISHED,
            assistant_turn_id="assistant-new",
            assistant_text_signature="sig",
            assistant_text="complete reply",
            assistant_count=1,
            user_count=2,
            user_turn_id="user-new",
            user_turn_pending=False,
        )

        result = bridge._accept_pending_reply(
            snapshot,
            target="https://chatgpt.com/c/00000000-0000-0000-0000-000000000001",
            pending={"packetId": "packet-1", "messageId": "user-new"},
        )

        self.assertEqual(result, {"accepted": True})
        self.assertEqual(cli.calls[0][0], "accept")
        self.assertEqual(cli.calls[0][1]["response"]["replyTo"], "user-new")
        self.assertEqual(cli.calls[0][1]["response"]["messageId"], "assistant-new")

    def test_active_assistant_reply_is_not_accepted_as_final(self) -> None:
        bridge, cli = self.bridge()
        snapshot = PageSnapshot(
            phase=Phase.RESPONDING,
            assistant_turn_id="assistant-new",
            assistant_text_signature="sig",
            assistant_text="partial reply",
            assistant_count=1,
            user_count=1,
            user_turn_id="user-new",
            user_turn_pending=False,
        )

        result = bridge._accept_pending_reply(
            snapshot,
            target="https://chatgpt.com/c/00000000-0000-0000-0000-000000000001",
            pending={"packetId": "packet-1", "messageId": "user-new"},
        )

        self.assertIsNone(result)
        self.assertEqual(cli.calls, [])

    def test_pending_user_turn_blocks_reply_acceptance_even_when_counts_match(self) -> None:
        bridge, cli = self.bridge()
        snapshot = PageSnapshot(
            phase=Phase.RESPONDING,
            assistant_turn_id="assistant-old",
            assistant_text_signature="sig",
            assistant_text="old assistant output",
            assistant_count=2,
            user_count=2,
            user_turn_id="user-new",
            user_turn_pending=True,
        )

        result = bridge._accept_pending_reply(
            snapshot,
            target="https://chatgpt.com/c/00000000-0000-0000-0000-000000000001",
            pending={"packetId": "packet-1", "messageId": "user-new"},
        )

        self.assertIsNone(result)
        self.assertEqual(cli.calls, [])


if __name__ == "__main__":
    unittest.main()
