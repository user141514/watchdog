from __future__ import annotations

from typing import Iterable, Protocol, TypeVar
from urllib.parse import urlparse

from .dom_snapshot import DOM_SNAPSHOT_JS
from .model import PageSnapshot, Phase
from .relay_page import RelayChatGPTPage


# Backward-compatible import surface. The production implementation is now the
# OMP relay adapter; this module no longer owns a Playwright browser lifecycle.
ChatGPTPage = RelayChatGPTPage
_DOM_SNAPSHOT_JS = DOM_SNAPSHOT_JS


class PageSelectionError(RuntimeError):
    pass


class _HasURL(Protocol):
    url: str


P = TypeVar("P", bound=_HasURL)


def _is_chatgpt_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host == "chatgpt.com" or host.endswith(".chatgpt.com")


def select_unique_page(pages: Iterable[P], match_url: str) -> P:
    matches = [
        page
        for page in pages
        if _is_chatgpt_url(page.url) and match_url in page.url
    ]
    if not matches:
        raise PageSelectionError(
            f"no matching ChatGPT page for URL substring {match_url!r}"
        )
    if len(matches) > 1:
        urls = ", ".join(page.url for page in matches)
        raise PageSelectionError(
            f"multiple matching ChatGPT pages; use a more specific --match-url: {urls}"
        )
    return matches[0]


def continuation_was_accepted(before: PageSnapshot, current: PageSnapshot) -> bool:
    return (
        current.turn_key != before.turn_key
        or current.phase in (Phase.THINKING, Phase.RESPONDING)
    )
