"""System gate: real daemon + local owner/Relay counters, no production browser."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import subprocess
import sys
from threading import Lock, Thread
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
ID = "00000000-0000-0000-0000-000000000abc"
TARGET = f"https://chatgpt.com/c/{ID}"


class Probe:
    def __init__(self, owner=False):
        self.requests = []
        self.lock = Lock()
        probe = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                self.reply()

            def do_POST(self):
                self.reply()

            def reply(self):
                count = int(self.headers.get("content-length", 0))
                payload = json.loads(self.rfile.read(count)) if count else {}
                with probe.lock:
                    probe.requests.append((self.command, self.path, payload))
                if owner and self.path == "/internal/watchdog-bind":
                    body = {"accepted": True, **payload}
                elif owner and self.path == "/internal/watchdog-withdraw":
                    body = {"accepted": True, "quiescent": True, **payload}
                elif owner and self.path == "/internal/conversation-observation":
                    body = {"found": False, "reason": "isolated_test_no_observation"}
                else:
                    body = {"accepted": False, "reason": "unexpected_effect_route"}
                encoded = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port

    def snapshot(self):
        with self.lock:
            return list(self.requests)

    def clear(self):
        with self.lock:
            self.requests.clear()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


def api(port, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = Request(f"http://127.0.0.1:{port}{path}", data=data,
                  headers={"content-type": "application/json"})
    with urlopen(req, timeout=3) as response:
        return json.load(response)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def await_condition(check, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.02)
    raise AssertionError("system gate condition did not become true")


def start_daemon(store, owner, relay):
    port = free_port()
    process = subprocess.Popen([
        sys.executable, "-I", "-c",
        "import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); "
        "runpy.run_module('chat_watchdog',run_name='__main__')",
        str(ROOT), "--simple", "--registry-port", str(port),
        "--registry-store", str(store), "--simple-interval-seconds", "0.03",
        "--relay-url", f"http://127.0.0.1:{relay.port}",
        "--intent-url", f"http://127.0.0.1:{owner.port}/internal/conversation-intents",
    ], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    def ready():
        if process.poll() is not None:
            raise AssertionError(process.stderr.read().decode(errors="replace"))
        try:
            return api(port, "/health")["ready"]
        except (URLError, OSError, TimeoutError):
            return False
    try:
        await_condition(ready)
    except BaseException:
        stop_daemon(process)
        raise
    health = api(port, "/health")
    assert Path(health["module_path"]).resolve().is_relative_to(ROOT.resolve())
    return process, port


def stop_daemon(process):
    if process and process.poll() is None:
        process.kill()
        process.wait(3)
    if process and process.stderr:
        process.stderr.close()


def assert_silent_window(port, owner, relay):
    # More than eight ACTIVE polling intervals; EMPTY should wait indefinitely.
    time.sleep(0.25)
    health = api(port, "/health")
    assert health["quiescent"] is True
    assert health["active_count"] == 0
    assert health["pending_withdrawal_count"] == 0
    assert health["scheduler_state"] == "empty"
    assert owner.snapshot() == []
    assert relay.snapshot() == []


def test_empty_bind_withdraw_restart_never_touches_browser(tmp_path):
    owner, relay = Probe(owner=True), Probe()
    process = None
    store = tmp_path / "isolated-registry.sqlite3"
    try:
        process, port = start_daemon(store, owner, relay)
        assert_silent_window(port, owner, relay)
        bound = api(port, "/register", {
            "url": TARGET, "explicit": True, "source": "controlled-api",
            "actor": "system-gate", "operation_id": "gate-bind",
            "reason": "isolated lifecycle acceptance",
        })
        assert bound["created"] is True
        await_condition(lambda: any(path == "/internal/conversation-observation"
                                   for _, path, _ in owner.snapshot()))
        assert relay.snapshot() == []
        assert {path for _, path, _ in owner.snapshot()} <= {
            "/internal/watchdog-bind", "/internal/conversation-observation",
        }
        removed = api(port, "/unregister", {
            "conversation_id": ID, "source": "controlled-api",
            "actor": "system-gate", "operation_id": "gate-unbind",
            "reason": "isolated lifecycle acceptance",
        })
        assert removed["removed"] is True
        assert api(port, "/watches")["watches"] == []
        owner.clear()
        assert_silent_window(port, owner, relay)

        try:
            api(port, "/register", {"url": TARGET})
        except HTTPError as error:
            assert error.code == 409
            assert json.load(error)["error"] == "explicit_bind_required"
        else:
            raise AssertionError("implicit registration unexpectedly accepted")
        assert_silent_window(port, owner, relay)

        stop_daemon(process)
        process, port = start_daemon(store, owner, relay)
        assert api(port, "/watches")["watches"] == []
        assert_silent_window(port, owner, relay)
        history = api(port, "/lifecycle")["events"]
        assert history[0]["operation"] == "unregister"
        assert history[0]["operation_id"] == "gate-unbind"
        assert history[-1]["operation_id"] == "gate-bind"
    finally:
        stop_daemon(process)
        owner.close()
        relay.close()
