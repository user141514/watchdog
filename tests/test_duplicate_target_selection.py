from __future__ import annotations

import pytest

from chat_watchdog.relay_page import RelayTargetSelectionError, select_unique_target


def test_exact_duplicate_conversation_tabs_choose_original_target() -> None:
    url = "https://chatgpt.com/g/g-p-test/c/00000000-0000-0000-0000-000000000123"
    targets = [
        {"id": "PAGE100", "type": "page", "url": url},
        {"id": "PAGE200", "type": "page", "url": url},
    ]

    selected = select_unique_target(targets, "/c/00000000-0000-0000-0000-000000000123")

    assert selected["id"] == "PAGE100"


def test_distinct_matching_urls_remain_ambiguous() -> None:
    targets = [
        {
            "id": "PAGE100",
            "type": "page",
            "url": "https://chatgpt.com/c/00000000-0000-0000-0000-000000000123",
        },
        {
            "id": "PAGE200",
            "type": "page",
            "url": "https://chatgpt.com/g/g-p-test/c/00000000-0000-0000-0000-000000000123",
        },
    ]

    with pytest.raises(RelayTargetSelectionError, match="multiple matching ChatGPT targets"):
        select_unique_target(targets, "/c/00000000-0000-0000-0000-000000000123")
