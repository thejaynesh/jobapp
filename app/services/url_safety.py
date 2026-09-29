"""
Refuse to fetch internal addresses on behalf of a job posting.

Link resolution, enrichment, liveness checks and the careers-site sniffer all
fetch URLs that came out of third-party postings, and follow their redirects.
Anyone who can publish a posting on a board this reads chooses those URLs — so
without a check, a posting could point the server at `http://redis:6379`, the
app's own `web:8000`, a router's admin page, or a cloud metadata endpoint, and
whatever came back would be stored as that job's description.

The request hook rejects local names and unsupported schemes on every hop.
PublicNetworkBackend validates DNS when opening each socket and connects to
the checked numeric address while HTTPcore retains TLS hostname verification.
"""

import ipaddress
import logging
import socket
import ssl
import time

import httpx
import httpcore

logger = logging.getLogger(__name__)


class UnsafeDestination(httpx.RequestError):
    """A fetch that would reach a private, loopback or internal address."""


def _resolve(host: str) -> list[str]:
    """Every address `host` resolves to. Separate so tests can replace it."""
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return [info[4][0] for info in infos]


def _is_public_ip(text: str) -> bool:
    try:
        address = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return (address.is_global and not address.is_multicast and not address.is_reserved
            and not getattr(address, "is_site_local", False))


def _allowed_name(host: str) -> bool:
    """Reject local names and nonpublic literals without a DNS lookup."""
    host = (host or "").strip().strip("[]").rstrip(".").lower()
    if not host:
        return False
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
        return _is_public_ip(host)
    except ValueError:
        pass
    if host == "localhost" or host.endswith(".localhost") or "." not in host:
        return False
    if host.endswith((".internal", ".local", ".lan", ".home.arpa")):
        return False
    return True


def _public_addresses(host: str) -> list[str]:
    if not _allowed_name(host):
        raise UnsafeDestination(f"refused to fetch {host}: not a public address")
    try:
        addresses = [str(ipaddress.ip_address(host.strip("[]")))]
    except ValueError:
        try:
            addresses = _resolve(host)
        except OSError as exc:
            raise UnsafeDestination(f"could not safely resolve {host}") from exc
    if not addresses or not all(_is_public_ip(a) for a in addresses):
        raise UnsafeDestination(f"refused to fetch {host}: not a public address")
    return list(dict.fromkeys(addresses))


def is_public_host(host: str) -> bool:
    """Inspect current DNS; a failed lookup never grants permission to connect."""
    try:
        _public_addresses(host)
        return True
    except UnsafeDestination:
        return False


class PublicNetworkBackend(httpcore.SyncBackend):
    """Connect to validated numeric addresses, preserving the TLS origin.

    HTTPcore retains the original hostname for Host, SNI and certificate checks.
    Reused sockets already have a validated peer. Every new connection resolves
    again, including connections created after a redirect or dropped socket.
    """

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        addresses = _public_addresses(host)
        deadline = None if timeout is None else time.monotonic() + timeout
        last_error = None
        for address in addresses:
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            try:
                return super().connect_tcp(address, port, timeout=remaining,
                                           local_address=local_address, socket_options=socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        raise last_error

    def connect_unix_socket(self, *args, **kwargs):
        raise UnsafeDestination("Unix sockets are not public destinations")


class PublicTransport(httpx.HTTPTransport):
    """HTTPX stream/exception handling with a destination-enforcing pool."""

    def __init__(self):
        super().__init__(trust_env=False)
        self._pool.close()
        self._pool = httpcore.ConnectionPool(
            ssl_context=ssl.create_default_context(), network_backend=PublicNetworkBackend(),
            max_connections=20, max_keepalive_connections=10,
        )


def public_client(**kwargs) -> httpx.Client:
    # A proxy could resolve the name somewhere else, bypassing validation.
    return httpx.Client(**kwargs, transport=PublicTransport(), trust_env=False,
                        event_hooks=EVENT_HOOKS)


def is_public_url(url: str) -> bool:
    try:
        parsed = httpx.URL(url)
    except Exception:
        return False
    return parsed.scheme in ("http", "https") and is_public_host(parsed.host)


def guard_request(request: httpx.Request) -> None:
    """httpx `request` event hook: raise before connecting anywhere internal."""
    if request.url.scheme not in ("http", "https") or not _allowed_name(request.url.host):
        logger.warning("url_safety: refused to fetch %s (not a public address)",
                       request.url.host)
        raise UnsafeDestination(
            f"refused to fetch {request.url.host}: not a public address",
            request=request,
        )


EVENT_HOOKS = {"request": [guard_request]}
