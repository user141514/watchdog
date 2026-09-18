from __future__ import annotations

import json
import sqlite3
from threading import Event, Thread
from urllib.request import urlopen

import pytest

from chat_watchdog.cli import build_parser
from chat_watchdog.registry import WatchRegistry, create_control_server
from test_registry_lifecycle import URL_A, Watcher


def test_cli_registry_is_durable_by_default_and_path_can_be_pinned(tmp_path):
    args = build_parser().parse_args(["--registry-port", "9235"])
    assert args.registry_store
    explicit = str(tmp_path / "isolated.sqlite3")
    args = build_parser().parse_args(["--registry-port", "9236", "--registry-store", explicit])
    assert args.registry_store == explicit


def test_durable_registry_refuses_unbrokered_automatic_replay(monkeypatch):
    from chat_watchdog import cli

    monkeypatch.setattr(cli, "_run_registry_mode", lambda *args: pytest.fail("unsafe legacy registry started"))
    with pytest.raises(SystemExit, match="single-conversation"):
        cli.main(["--registry-port", "9235", "--legacy-direct-send"])


def test_health_http_includes_runtime_identity_and_polling_evidence():
    registry = WatchRegistry(lambda url: Watcher())
    registry.step_all()
    server = create_control_server(registry, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(f"http://127.0.0.1:{server.server_port}/health", timeout=2) as response:
            value = json.load(response)
        assert value["ready"]
        assert value["instance_id"]
        assert value["pid"] > 0
        assert value["module_path"].endswith("registry.py")
        assert value["last_poll_completed_at"] is not None
        assert value["durable"] is False
    finally:
        server.shutdown()
        thread.join(2)
        server.server_close()
        registry.close()


def test_watch_list_exposes_disconnection_instead_of_hiding_it():
    def offline(url):
        raise ConnectionError("relay temporarily absent")

    registry = WatchRegistry(offline)
    registry.register(URL_A)
    server = create_control_server(registry, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(f"http://127.0.0.1:{server.server_port}/watches", timeout=2) as response:
            item = json.load(response)["watches"][0]
        assert item["state"] == "reconnecting"
        assert item["connected"] is False
        assert item["consecutive_failures"] == 1
        assert "relay temporarily absent" in item["last_error"]
    finally:
        server.shutdown()
        thread.join(2)
        server.server_close()
        registry.close()


def test_control_server_can_answer_health_while_another_client_is_stalled():
    import socket

    registry = WatchRegistry(lambda url: Watcher())
    registry.step_all()
    server = create_control_server(registry, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    stalled = socket.create_connection(server.server_address, timeout=2)
    try:
        # An incomplete request must not monopolize the single control accept loop.
        stalled.sendall(b"POST /register HTTP/1.1\r\nHost: localhost\r\n")
        with urlopen(f"http://127.0.0.1:{server.server_port}/health", timeout=1) as response:
            assert json.load(response)["ready"]
    finally:
        stalled.close()
        server.shutdown()
        thread.join(2)
        server.server_close()
        registry.close()


def test_storage_failure_is_visible_and_prevents_any_watch_effect(monkeypatch):
    watcher = Watcher()
    registry = WatchRegistry(lambda url: watcher)
    registry.register(URL_A)
    registry.step_all()
    assert watcher.steps == 1
    observe = registry._store.observe

    def disk_full(entry):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(registry._store, "observe", disk_full)
    try:
        with pytest.raises(sqlite3.OperationalError, match="disk full"):
            registry.step_all()
        assert watcher.steps == 1
        assert registry.health()["ready"] is False
        assert "disk full" in registry.health()["last_poll_error"]
        monkeypatch.setattr(registry._store, "observe", observe)
        registry.step_all()
        assert watcher.steps == 2
        assert registry.health()["ready"] is True
    finally:
        registry.close()
