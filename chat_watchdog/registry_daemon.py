from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Callable, Protocol

from .registry import WatchEntry, WatchRegistry
from .supervisor import StepResult


class SupervisorPort(Protocol):
    should_stop: bool

    def step(self) -> StepResult: ...


class WatchRuntimePort(Protocol):
    supervisor: SupervisorPort

    def close(self) -> None: ...


RuntimeFactory = Callable[[WatchEntry], WatchRuntimePort]


@dataclass
class _RuntimeSlot:
    signature: tuple[str, str | None, str | None]
    runtime: WatchRuntimePort


def _signature(entry: WatchEntry) -> tuple[str, str | None, str | None]:
    return (entry.target_url, entry.reanchor_scope, entry.reanchor_epoch)


class WatchDaemon:
    def __init__(
        self,
        registry: WatchRegistry,
        runtime_factory: RuntimeFactory,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._registry = registry
        self._runtime_factory = runtime_factory
        self._logger = logger or logging.getLogger(__name__)
        self._runtimes: dict[str, _RuntimeSlot] = {}

    @property
    def runtime_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._runtimes))

    def _close_runtime(self, watch_id: str) -> None:
        slot = self._runtimes.pop(watch_id, None)
        if slot is None:
            return
        try:
            slot.runtime.close()
        except Exception:
            self._logger.exception("failed to close watch runtime %s", watch_id)

    def reconcile(self) -> None:
        active_entries = {entry.watch_id: entry for entry in self._registry.list(state="active")}

        for watch_id, slot in list(self._runtimes.items()):
            entry = active_entries.get(watch_id)
            if entry is None or slot.signature != _signature(entry):
                self._close_runtime(watch_id)

        for watch_id, entry in active_entries.items():
            if watch_id in self._runtimes:
                continue
            try:
                runtime = self._runtime_factory(entry)
            except Exception:
                self._logger.exception(
                    "failed to create watch runtime %s for %s",
                    watch_id,
                    entry.target_url,
                )
                continue
            self._runtimes[watch_id] = _RuntimeSlot(
                signature=_signature(entry),
                runtime=runtime,
            )

    def step(self) -> dict[str, StepResult]:
        self.reconcile()
        results: dict[str, StepResult] = {}

        for watch_id in list(sorted(self._runtimes)):
            slot = self._runtimes.get(watch_id)
            if slot is None:
                continue
            try:
                result = slot.runtime.supervisor.step()
            except Exception:
                self._logger.exception("watch runtime %s failed during step", watch_id)
                self._close_runtime(watch_id)
                continue

            results[watch_id] = result
            try:
                self._registry.record_result(watch_id, result.value)
            except KeyError:
                self._close_runtime(watch_id)
                continue

            if slot.runtime.supervisor.should_stop or result is StepResult.DONE:
                try:
                    self._registry.set_state(watch_id, "completed")
                except KeyError:
                    pass
                self._close_runtime(watch_id)

        return results

    def close(self) -> None:
        for watch_id in list(self._runtimes):
            self._close_runtime(watch_id)
