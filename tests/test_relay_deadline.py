import json

import pytest

from chat_watchdog.relay_cdp import RelayCdpError, RelayCdpProtocol
from chat_watchdog.relay_page import RelayChatGPTPage


class Clock:
    value = 0.0

    def __call__(self):
        return self.value


class EventStream:
    def __init__(self, clock):
        self.clock = clock
        self.messages = []
        self.reads = 0
        self.timeout = None
        self.send_budgets = []
        self.match = False

    def settimeout(self, timeout):
        self.timeout = timeout

    def send(self, payload):
        self.send_budgets.append(self.timeout)
        self.messages.append(json.loads(payload))

    def recv(self):
        self.reads += 1
        if self.reads > 20:
            raise AssertionError("response wait ignored its total deadline")
        self.clock.value += 0.4
        if self.match:
            return json.dumps({"id": self.messages[-1]["id"], "result": {"result": {"value": 7}}})
        return json.dumps({"method": "Runtime.consoleAPICalled", "params": {}})


@pytest.mark.parametrize("method", ["attach", "evaluate"])
def test_unrelated_event_stream_cannot_keep_a_request_alive_forever(method):
    clock = Clock()
    socket = EventStream(clock)
    protocol = RelayCdpProtocol(socket, request_timeout=1.0, clock=clock)
    with pytest.raises(RelayCdpError, match="deadline"):
        if method == "attach":
            protocol.attach_target("PAGE-EXACT")
        else:
            protocol.evaluate("session-exact", "1")
    assert socket.reads <= 3
    assert len(socket.messages) == 1, "a timeout must not resend a CDP operation"


def test_generic_cdp_command_supports_target_create_and_page_reload():
    class CommandSocket:
        def __init__(self):
            self.messages = []
            self.timeout = None

        def settimeout(self, timeout):
            self.timeout = timeout

        def send(self, payload):
            self.messages.append(json.loads(payload))

        def recv(self):
            message = self.messages[-1]
            if message["method"] == "Target.createTarget":
                return json.dumps({"id": message["id"], "result": {"targetId": "PAGE-NEW"}})
            return json.dumps({"id": message["id"], "result": {}})

    socket = CommandSocket()
    protocol = RelayCdpProtocol(socket)
    assert protocol.create_target("https://chatgpt.com/c/10000000-0000-4000-8000-000000000001") == "PAGE-NEW"
    protocol.reload_page("session-1")

    assert socket.messages[0]["method"] == "Target.createTarget"
    assert socket.messages[1]["method"] == "Page.reload"
    assert socket.messages[1]["sessionId"] == "session-1"


def test_next_request_gets_a_fresh_socket_budget_without_replaying_prior_send():
    clock = Clock()
    socket = EventStream(clock)
    protocol = RelayCdpProtocol(socket, request_timeout=1.0, clock=clock)
    with pytest.raises(RelayCdpError, match="deadline"):
        protocol.evaluate("session", "first()")
    socket.match = True
    assert protocol.evaluate("session", "second()") == 7
    assert socket.send_budgets == [1.0, 1.0]
    assert [m["id"] for m in socket.messages] == [1, 2]


def test_a_matching_but_late_response_is_not_accepted():
    clock = Clock()
    socket = EventStream(clock)
    socket.match = True
    protocol = RelayCdpProtocol(socket, request_timeout=0.2, clock=clock)
    with pytest.raises(RelayCdpError, match="deadline"):
        protocol.evaluate("session", "mutation()")
    assert len(socket.messages) == 1


def test_send_disconnection_uses_the_reconnectable_transport_error_type():
    class Disconnected:
        def send(self, payload):
            raise OSError("relay connection closed")

    with pytest.raises(RelayCdpError, match="relay connection closed"):
        RelayCdpProtocol(Disconnected()).evaluate("session", "1")


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_request_budget_must_be_finite_and_positive(timeout):
    with pytest.raises(ValueError, match="positive.*finite|finite.*positive"):
        RelayCdpProtocol(object(), request_timeout=timeout)


def test_reconnect_keeps_a_valid_replacement_when_old_socket_cleanup_fails(monkeypatch):
    class OldSocket:
        def close(self):
            raise OSError("old socket already broken")

    replacement = object()
    target = "https://chatgpt.com/c/10000000-0000-4000-8000-000000000001"
    fetch = lambda url: ([{"id": "PAGE-NEW", "type": "page", "url": target}]
                         if url.endswith("/json/list") else
                         {"webSocketDebuggerUrl": "ws://127.0.0.1:1/fixture"})
    old = OldSocket()
    page = RelayChatGPTPage(
        "PAGE-OLD", target, "session-old", old, RelayCdpProtocol(old), target,
        relay_url="http://127.0.0.1:1", fetch_json=fetch,
        websocket_factory=lambda *args, **kwargs: replacement,
    )
    monkeypatch.setattr(RelayCdpProtocol, "attach_target", lambda *args: "session-new")
    assert page._reconnect()
    assert page.socket is replacement
    assert page.target_id == "PAGE-NEW"
    assert page.session_id == "session-new"


def test_initial_attach_failure_closes_the_created_socket(monkeypatch):
    class Socket:
        closed = False

        def close(self):
            self.closed = True

    socket = Socket()
    target = "https://chatgpt.com/c/10000000-0000-4000-8000-000000000001"

    def fetch(url):
        if url.endswith("/json/list"):
            return [{"id": "PAGE-EXACT", "type": "page", "url": target}]
        return {"webSocketDebuggerUrl": "ws://127.0.0.1:1/fixture"}

    def fail_attach(self, target_id):
        raise RelayCdpError("attach failed")

    monkeypatch.setattr(RelayCdpProtocol, "attach_target", fail_attach)
    with pytest.raises(RelayCdpError, match="attach failed"):
        RelayChatGPTPage.connect(
            "http://127.0.0.1:1", target, fetch_json=fetch,
            websocket_factory=lambda *args, **kwargs: socket,
        )
    assert socket.closed
