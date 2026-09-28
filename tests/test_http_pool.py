"""
`httpx.get` and friends through one connection pool, behaving exactly as
before otherwise. Against a server on 127.0.0.1 in this process: nothing
leaves the machine.
"""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from app.services import http_pool


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"   # keep-alive, so reuse is possible at all
    connections: set = set()
    cookies_seen: list = []

    def log_message(self, *args):
        pass

    def _reply(self, status=200, body=b"ok", headers=()):
        self.send_response(status)
        for key, value in headers:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        type(self).connections.add(self.client_address)
        type(self).cookies_seen.append(self.headers.get("Cookie"))
        if self.path == "/set-cookie":
            self._reply(headers=[("Set-Cookie", "session=abc; Path=/")])
        elif self.path == "/bounce":
            # Avature's shape: a redirect to itself that only works with the cookie.
            if "session=abc" in (self.headers.get("Cookie") or ""):
                self._reply(body=b"posting")
            else:
                self._reply(302, b"", [("Location", "/bounce"),
                                       ("Set-Cookie", "session=abc; Path=/")])
        else:
            self._reply()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        type(self).connections.add(self.client_address)
        self._reply(body=body)


@pytest.fixture
def server(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    http_pool._after_fork()           # a pool built without the proxy settings
    _Handler.connections = set()
    _Handler.cookies_seen = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    http_pool.install()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    http_pool.uninstall()
    httpd.shutdown()
    http_pool._after_fork()


def test_requests_to_one_host_share_a_connection(server):
    for _ in range(5):
        assert httpx.get(server + "/", timeout=5).text == "ok"
    assert len(_Handler.connections) == 1


def test_without_the_pool_each_request_opened_its_own(server):
    http_pool.uninstall()
    for _ in range(3):
        httpx.get(server + "/", timeout=5)
    assert len(_Handler.connections) == 3


def test_cookies_do_not_carry_from_one_call_to_the_next(server):
    httpx.get(server + "/set-cookie", timeout=5)
    httpx.get(server + "/", timeout=5)
    assert _Handler.cookies_seen == [None, None]


def test_a_redirect_that_sets_a_cookie_is_followed_with_it(server):
    resp = httpx.get(server + "/bounce", follow_redirects=True, timeout=5)
    assert resp.text == "posting" and resp.url.path == "/bounce"


def test_post_and_stream(server):
    assert httpx.post(server + "/echo", content=b"hello", timeout=5).text == "hello"
    with httpx.stream("GET", server + "/", timeout=5) as resp:
        assert resp.read() == b"ok"
    assert len(_Handler.connections) == 1


def test_unusual_options_take_the_original_path(server, monkeypatch):
    seen = []
    original = http_pool._ORIGINAL["request"]
    monkeypatch.setitem(http_pool._ORIGINAL, "request",
                        lambda method, url, **kw: seen.append(kw) or original(method, url, **kw))
    httpx.get(server + "/", verify=False, timeout=5)
    httpx.get(server + "/", timeout=5)
    assert len(seen) == 1 and seen[0]["verify"] is False


def test_a_test_that_fakes_httpx_get_still_fakes_it(server):
    pooled = httpx.get
    httpx.get = lambda url, **kw: httpx.Response(418, request=httpx.Request("GET", url))
    try:
        assert httpx.get(server + "/").status_code == 418
    finally:
        httpx.get = pooled
    assert _Handler.connections == set()


def test_a_get_on_a_connection_closed_at_the_far_end_is_asked_again(server, monkeypatch):
    real = http_pool._SharedTransport.handle_request
    failures = {"left": 1}

    def flaky(self, request):
        if failures["left"]:
            failures["left"] -= 1
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return real(self, request)

    monkeypatch.setattr(http_pool._SharedTransport, "handle_request", flaky)
    assert httpx.get(server + "/", timeout=5).text == "ok"
    failures["left"] = 1
    with pytest.raises(httpx.RemoteProtocolError):
        httpx.post(server + "/echo", content=b"once", timeout=5)   # never sent twice


def test_a_forked_child_builds_its_own_pool(server):
    httpx.get(server + "/", timeout=5)
    first = http_pool._pool()
    http_pool._after_fork()
    assert http_pool._pool() is not first
