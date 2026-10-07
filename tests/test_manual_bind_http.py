from contextlib import contextmanager
import json
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from chat_watchdog.registry import WatchRegistry, create_control_server
from test_manual_rebind_closure import FakeWatcher, CID, URL


@contextmanager
def control(tmp_path, projection):
    events = []
    registry = WatchRegistry(lambda _: FakeWatcher(events), store_path=tmp_path / "http.sqlite3",
                             connect_on_register=False)
    server = create_control_server(registry, port=0, register_projection=projection,
                                   rebind_on_register=True, wake=lambda: events.append(("wake", None)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def bind(op):
        payload = {"url": URL, "explicit": True, "source": "observatory-ui", "actor": "human",
                   "operation_id": op, "reason": "manual-bind"}
        with urlopen(Request(f"http://127.0.0.1:{server.server_port}/register",
                     data=json.dumps(payload).encode(), headers={"content-type": "application/json"}), timeout=3) as r:
            return json.load(r)
    try:
        yield registry, events, bind
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        registry.close()


def test_http_manual_rebind_reports_two_separate_receipts_and_rebuilds_normal(tmp_path):
    assert "rebind_on_register" in __import__("inspect").signature(create_control_server).parameters
    projected = []
    with control(tmp_path, lambda target: projected.append(target)) as (registry, events, bind):
        first = bind("first")
        assert first["mechanical_attached"] is True
        assert first["normal_binding"]["status"] == "pending"
        assert first["operation_id"] == "first"
        registry.step_all()
        before = registry.list()[0]
        events.clear()
        second = bind("second")
        assert second["created"] is False
        assert second["normal_binding"]["status"] == "pending"
        registry.step_all()
        assert ("close", None) in events
        assert ("bind", before.registration_id) in events
        assert ("observe-only", None) in events
        assert registry.list()[0].normal_binding["operation_id"] == "second"
        assert projected == [URL, URL]


def test_mechanical_failure_does_not_cancel_normal_rebind(tmp_path):
    assert "rebind_on_register" in __import__("inspect").signature(create_control_server).parameters
    def fail(_):
        raise RuntimeError("scheduler rejected")
    with control(tmp_path, fail) as (registry, events, bind):
        with pytest.raises(HTTPError) as error:
            bind("partial")
        result = json.load(error.value)
        assert error.value.code == 503
        assert result["mechanical_attached"] is False
        assert result["normal_binding"]["status"] == "pending"
        assert ("wake", None) in events
        registry.step_all()
        assert registry.list()[0].normal_binding["status"] == "observed"
