from __future__ import annotations

from threading import Event
from types import SimpleNamespace

from chat_watchdog import cli


def test_registry_poll_reconciles_independent_fixed_action_projection():
    class Registry:
        def __init__(self):
            self.stepped = 0

        def step_all(self):
            self.stepped += 1

        def list(self):
            return [SimpleNamespace(target_url="https://chatgpt.com/c/6ac34d31-2cc8-83ec-a770-430855a5d07d")]

        def has_scheduler_work(self):
            return True

    class Scheduler:
        def __init__(self):
            self.snapshots = []

        def reconcile_current(self, load_registrations):
            self.snapshots.append([item.target_url for item in load_registrations()])

    registry = Registry()
    scheduler = Scheduler()
    wake = Event()

    cli._registry_poll_cycle(
        registry,
        wake,
        0.01,
        fixed_action_scheduler=scheduler,
    )

    assert registry.stepped == 1
    assert scheduler.snapshots == [[
        "https://chatgpt.com/c/6ac34d31-2cc8-83ec-a770-430855a5d07d"
    ]]
