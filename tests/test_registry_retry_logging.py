"""Observable retry status stays current without repeating the same warning."""
from __future__ import annotations

import logging
import sqlite3

from chat_watchdog.registry import WatchRegistry

CID = "6aa542fd-708c-83ea-869a-721efd83d7f3"
TARGET = f"https://chatgpt.com/c/{CID}"


class RetryWatcher:
    should_stop = False
    completion_text = None
    state = "active"

    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)

    def step(self):
        outcome = next(self.outcomes)
        if outcome is not None:
            raise outcome

    def close(self):
        pass


def warnings(caplog):
    return [record for record in caplog.records
            if record.name == "chat_watchdog.registry" and record.levelno == logging.WARNING]


def test_identical_errors_warn_once_but_every_failure_is_persisted(caplog, tmp_path):
    watcher = RetryWatcher([RuntimeError("relay unavailable")] * 3)
    path = tmp_path / "registry.sqlite3"
    registry = WatchRegistry(lambda target: watcher, store_path=path, connect_on_register=False)
    try:
        registry.register(TARGET)
        with caplog.at_level(logging.INFO, logger="chat_watchdog.registry"):
            for _ in range(3):
                registry.step_all()
        assert len(warnings(caplog)) == 1
        row = registry.list()[0]
        assert row.state == "degraded"
        assert row.consecutive_failures == 3
        assert row.last_error == "RuntimeError: relay unavailable"
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT consecutive_failures, last_error FROM watch_records").fetchone() == (
                3, "RuntimeError: relay unavailable")
    finally:
        registry.close()


def test_changed_error_type_or_reason_always_warns(caplog):
    watcher = RetryWatcher([
        RuntimeError("owner unavailable"), RuntimeError("owner unavailable"),
        RuntimeError("owner changed"), ValueError("owner changed"),
    ])
    registry = WatchRegistry(lambda target: watcher, connect_on_register=False)
    try:
        registry.register(TARGET)
        with caplog.at_level(logging.INFO, logger="chat_watchdog.registry"):
            for _ in range(4):
                registry.step_all()
        assert len(warnings(caplog)) == 3
        assert "RuntimeError: owner unavailable" in warnings(caplog)[0].getMessage()
        assert "RuntimeError: owner changed" in warnings(caplog)[1].getMessage()
        assert "ValueError: owner changed" in warnings(caplog)[2].getMessage()
        assert registry.list()[0].consecutive_failures == 4
    finally:
        registry.close()


def test_recovery_logs_once_and_same_later_failure_warns_again(caplog):
    watcher = RetryWatcher([
        RuntimeError("owner unavailable"), None, None, RuntimeError("owner unavailable"),
    ])
    registry = WatchRegistry(lambda target: watcher, connect_on_register=False)
    try:
        registry.register(TARGET)
        with caplog.at_level(logging.INFO, logger="chat_watchdog.registry"):
            registry.step_all()
            registry.step_all()
            recovered = registry.list()[0]
            assert recovered.last_error is None
            assert recovered.consecutive_failures == 0
            registry.step_all()
            registry.step_all()
        assert len(warnings(caplog)) == 2
        recoveries = [record for record in caplog.records
                      if record.name == "chat_watchdog.registry"
                      and record.levelno == logging.INFO and "recovered" in record.getMessage()]
        assert len(recoveries) == 1
        assert CID in recoveries[0].getMessage()
        assert "RuntimeError: owner unavailable" in recoveries[0].getMessage()
        assert registry.list()[0].consecutive_failures == 1
    finally:
        registry.close()


def test_unknown_error_details_are_not_truncated_or_hidden(caplog):
    prefix = "unexpected: " + "x" * 1200
    watcher = RetryWatcher([RuntimeError(prefix + "first"), RuntimeError(prefix + "second")])
    registry = WatchRegistry(lambda target: watcher, connect_on_register=False)
    try:
        registry.register(TARGET)
        with caplog.at_level(logging.INFO, logger="chat_watchdog.registry"):
            registry.step_all()
            registry.step_all()
        assert len(warnings(caplog)) == 2
        assert registry.list()[0].last_error == "RuntimeError: " + prefix + "second"
        assert warnings(caplog)[1].getMessage().endswith(prefix + "second")
        assert registry.list()[0].state == "degraded"
    finally:
        registry.close()


def test_missing_native_turn_identity_is_visible_and_preserves_desired_provenance(caplog, tmp_path):
    def unavailable(target):
        raise RuntimeError("persistent_turn_identity_unavailable")

    path = tmp_path / "registry.sqlite3"
    provenance = {"source": "cli-explicit", "actor": "human", "operation_id": "manual-bind",
                  "reason": "explicit requested supervision"}
    registry = WatchRegistry(unavailable, store_path=path, connect_on_register=False)
    registry.register(TARGET, provenance=provenance)
    initial = registry.list()[0]
    history = registry.lifecycle()
    try:
        with caplog.at_level(logging.INFO, logger="chat_watchdog.registry"):
            for _ in range(3):
                registry.step_all()
        row = registry.list()[0]
        assert len(warnings(caplog)) == 1
        assert row.state == "observation_unavailable"
        assert row.diagnostics["reason"] == "persistent_turn_identity_unavailable"
        assert row.diagnostics["observation_available"] is False
        assert row.connected is False
        assert row.consecutive_failures == 3
        assert row.last_error == "RuntimeError: persistent_turn_identity_unavailable"
        assert row.registration_id == initial.registration_id
        assert row.registered_at == initial.registered_at
        assert row.last_registration == initial.last_registration
        assert registry.lifecycle() == history
    finally:
        registry.close()
    restored = WatchRegistry(unavailable, store_path=path, connect_on_register=False)
    try:
        row = restored.list()[0]
        assert row.state == "observation_unavailable"
        assert row.diagnostics["reason"] == "persistent_turn_identity_unavailable"
        assert row.registration_id == initial.registration_id
        assert row.last_registration == initial.last_registration
        assert restored.lifecycle() == history
    finally:
        restored.close()


def test_new_explicit_generation_does_not_inherit_previous_warning_suppression(caplog):
    registry = WatchRegistry(
        lambda target: RetryWatcher([RuntimeError("owner unavailable")]),
        connect_on_register=False,
    )
    try:
        registry.register(TARGET, provenance={"reason": "first explicit bind"})
        first = registry.list()[0]
        with caplog.at_level(logging.INFO, logger="chat_watchdog.registry"):
            registry.step_all()
            assert registry.unregister(CID) is True
            registry.register(TARGET, provenance={"reason": "second explicit bind"})
            second = registry.list()[0]
            registry.step_all()
        assert len(warnings(caplog)) == 2
        assert second.registration_id != first.registration_id
        assert first.last_registration["reason"] == "first explicit bind"
        assert second.last_registration["reason"] == "second explicit bind"
    finally:
        registry.close()
