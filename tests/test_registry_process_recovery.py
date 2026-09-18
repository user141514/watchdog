"""Real Windows/POSIX process and HTTP tests; never contact a browser or Sidecar."""
from __future__ import annotations

import json
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

from chat_watchdog.registry import WatchRegistry
from test_registry_lifecycle import ID_A, URL_A, Watcher


ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request(port, path, body=None, *, timeout=5):
    data = None if body is None else json.dumps(body).encode()
    req = Request(f"http://127.0.0.1:{port}{path}", data=data,
                  headers={"Content-Type": "application/json"})
    # Withdrawal fences any in-flight transport operation (bounded by its timeout).
    with urlopen(req, timeout=timeout) as response:
        return json.load(response)


def start_daemon(store, port):
    process = subprocess.Popen(
        [sys.executable, "-I", "-m", "chat_watchdog", "--registry-port", str(port),
         "--registry-store", str(store), "--relay-url", "http://127.0.0.1:1",
         "--poll-seconds", "0.05"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(process.stderr.read().decode(errors="replace"))
        try:
            if request(port, "/health")["ready"]:
                return process
        except (URLError, TimeoutError, OSError):
            time.sleep(0.05)
    process.kill()
    process.wait(3)
    raise AssertionError("isolated daemon never became healthy")


def kill(process):
    if process.poll() is None:
        process.kill()
    process.wait(timeout=3)
    if process.stderr:
        process.stderr.close()


def test_real_process_kill_restart_retains_offline_mount_and_resumes_polling(tmp_path):
    store, port = tmp_path / "registry.sqlite3", free_port()
    first = start_daemon(store, port)
    try:
        initial = request(port, "/health")
        result = request(port, "/register", {"url": URL_A}, timeout=1)
        assert result["created"]
        assert request(port, "/watches")["watches"][0]["conversation_id"] == ID_A
    finally:
        kill(first)  # Deliberately bypass close(), finally blocks and atexit handlers.
    second = start_daemon(store, port)
    try:
        recovered = request(port, "/health")
        assert recovered["instance_id"] != initial["instance_id"]
        assert recovered["pid"] > 0  # OS process IDs may legitimately be reused.
        assert recovered["durable"] is True
        item = request(port, "/watches")["watches"][0]
        assert item["conversation_id"] == ID_A
        assert item["target_url"] == URL_A
        assert item["state"] == "reconnecting"
        assert item["last_poll_at"] is not None
        assert item["consecutive_failures"] >= 1
        assert request(port, "/unregister", {"conversation_id": ID_A})["removed"]
    finally:
        kill(second)
    third = start_daemon(store, port)
    try:
        assert request(port, "/watches")["watches"] == []
    finally:
        kill(third)


def test_restart_does_not_reactivate_a_completed_receipt(tmp_path):
    store = tmp_path / "registry.sqlite3"
    original = WatchRegistry(lambda url: Watcher(should_stop=True), store_path=store)
    original.register(URL_A)
    original.step_all()
    original.close()
    port = free_port()
    process = start_daemon(store, port)
    try:
        receipt = request(port, "/completion", {"conversation_id": ID_A})
        assert receipt["active"] is False
        assert receipt["completed"] is True
        assert receipt["result"] == "verified final result"
        assert request(port, "/completion/ack", {"conversation_id": ID_A})["removed"]
    finally:
        kill(process)
    again = WatchRegistry(lambda url: Watcher(), store_path=store)
    try:
        assert again.completion(ID_A) is None
        assert again.list_ids() == []
    finally:
        again.close()
