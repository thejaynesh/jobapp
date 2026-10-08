"""
Connection reuse for every `httpx.get`, `httpx.post` and `httpx.stream`.

Forty-seven source adapters call `httpx.get` directly, and every such call
builds a throwaway `httpx.Client`: a fresh SSL context, a fresh TCP connection
and a fresh TLS handshake, closed again the moment the response is read. A
board cycle sends thousands of requests to the same few hosts —
boards-api.greenhouse.io, api.lever.co, the Workday clusters — so most of what
it waited on was handshakes it had already done.

`install()` swaps those three module functions for ones that behave exactly
the same — same arguments, a fresh client per call, so cookies and redirects
work as before (Avature's portals set a cookie on a redirect to themselves) —
except that the client sends through one long-lived connection pool per
process instead of opening its own. Nothing changes at the call sites.

It replaces the module attributes rather than asking every adapter to call
something new, for two reasons. The adapters stay as they are. And the tests,
which fake `httpx.get` in hundreds of places, keep faking exactly what
production calls: a test that patches `httpx.get` patches over this, and
never reaches the pool or the network.

Anything unusual — a custom `verify`, a client certificate, an explicit proxy,
`trust_env=False` — goes to the original function unchanged; the pool is for
the ordinary request, which is nearly all of them.

Fork-safe: Celery's prefork workers each build their own pool on first use,
and a pool inherited across `fork()` is discarded, since its sockets belong to
the parent.
"""

import logging
import os
import threading
from contextlib import contextmanager

import httpx

logger = logging.getLogger(__name__)

# Board fetches run a pool per source family and several families at once
# (`job_fetcher._run_all_adapters`), so the ceiling is well above httpx's
# default of 100. Idle connections are kept long enough to span a family's
# boards and no longer.
LIMITS = httpx.Limits(max_connections=256, max_keepalive_connections=128, keepalive_expiry=30.0)

_ORIGINAL = {"request": httpx.request, "get": httpx.get, "post": httpx.post, "stream": httpx.stream}
# Held here, as `httpx.get` holds its own reference: a test that patches
# `httpx.Client` for one module's client must not reach these calls either.
_Client = httpx.Client
_lock = threading.Lock()
_router: httpx.Client | None = None
_router_pid: int | None = None


def _pool() -> httpx.Client:
    """
    The process's long-lived client. Only its transports are used — the
    connection pool, and the proxy mounts `trust_env` builds from the
    environment — never its cookies.
    """
    global _router, _router_pid
    pid = os.getpid()
    if _router is None or _router_pid != pid:
        with _lock:
            if _router is None or _router_pid != pid:
                _router = _Client(limits=LIMITS, trust_env=True)
                _router_pid = pid
    return _router


class _SharedTransport(httpx.BaseTransport):
    """Sends through the pool. Closing it closes nothing: the pool outlives the call."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return _pool()._transport_for_url(request.url).handle_request(request)

    def close(self) -> None:
        pass


_SHARED = _SharedTransport()


def _unusual(kwargs: dict) -> bool:
    return bool(
        kwargs.get("proxy") or kwargs.get("proxies") or kwargs.get("cert")
        or kwargs.get("verify", True) is not True
        or kwargs.get("trust_env", True) is not True
    )


# httpx's own default for a one-off request.
_DEFAULT_TIMEOUT = httpx.Timeout(5.0)
_CLIENT_ONLY = ("proxy", "proxies", "cert", "verify", "trust_env")


def _client(kwargs: dict) -> httpx.Client:
    """
    The one-off client `httpx.get` would have made, but sending through the
    pool: with `transport=` it builds no SSL context and no proxy mounts of
    its own. Its cookie jar is its own, so nothing carries between calls.
    """
    for key in _CLIENT_ONLY:
        kwargs.pop(key, None)
    return _Client(
        cookies=kwargs.pop("cookies", None),
        timeout=kwargs.pop("timeout", _DEFAULT_TIMEOUT),
        transport=_SHARED,
    )


# A pooled connection can be closed at the far end between two requests, which
# a fresh one never is: the first request on it then fails before any answer.
# A GET changes nothing, so it is asked again, once, on a new connection.
_STALE = (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError, httpx.ConnectError)


def request(method: str, url, **kwargs) -> httpx.Response:
    if _unusual(kwargs):
        return _ORIGINAL["request"](method, url, **kwargs)
    # `_client` takes the client-level options out of `kwargs`; what is left
    # is the request, which can be sent twice.
    with _client(kwargs) as client:
        try:
            return client.request(method, url, **kwargs)
        except _STALE:
            if str(method).upper() not in ("GET", "HEAD"):
                raise
            return client.request(method, url, **kwargs)


def get(url, **kwargs) -> httpx.Response:
    return request("GET", url, **kwargs)


def post(url, **kwargs) -> httpx.Response:
    return request("POST", url, **kwargs)


@contextmanager
def stream(method: str, url, **kwargs):
    if _unusual(kwargs):
        with _ORIGINAL["stream"](method, url, **kwargs) as response:
            yield response
        return
    with _client(kwargs) as client, client.stream(method, url, **kwargs) as response:
        yield response


def install() -> None:
    """Route `httpx.request/get/post/stream` through the pool. Idempotent."""
    httpx.request = request
    httpx.get = get
    httpx.post = post
    httpx.stream = stream


def uninstall() -> None:
    for name, function in _ORIGINAL.items():
        setattr(httpx, name, function)


def _after_fork() -> None:
    global _router, _router_pid
    _router, _router_pid = None, None


# Windows starts independent worker processes and has no fork hook.
if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)
