"""Compatibility import for the simplified in-memory registry.

Dynamic watch lifecycle now lives in :mod:`chat_watchdog.registry` and the
registry mode in :mod:`chat_watchdog.cli`. This module intentionally owns no
second state machine or persistence layer.
"""

from .registry import WatchRegistry

__all__ = ["WatchRegistry"]
