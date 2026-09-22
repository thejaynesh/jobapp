"""
Refuse to fetch internal addresses on behalf of a job posting.

Link resolution, enrichment, liveness checks and the careers-site sniffer all
fetch URLs that came out of third-party postings, and follow their redirects.
Anyone who can publish a posting on a board this reads chooses those URLs — so
without a check, a posting could point the server at `http://redis:6379`, the
app's own `web:8000`, a router's admin page, or a cloud metadata endpoint, and
whatever came back would be stored as that job's description.

`guard_request` is an httpx request hook, so it runs on every hop of a
redirect chain rather than only on the first URL.
"""

import ipaddress
import logging
import socket
from functools import lru_cache

import httpx

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
    return address.is_global


@lru_cache(maxsize=4096)
def is_public_host(host: str) -> bool:
    """
    Whether every address this host name reaches is on the public internet.

    A single-label name ("redis", "web", "postgres") is a container or LAN
    name and is refused without a lookup. A name that does not resolve is let
    through: the request itself will fail, and refusing it here would only
    turn a DNS error into a more confusing one.
    """
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
    try:
        addresses = _resolve(host)
    except OSError:
        return True
    return bool(addresses) and all(_is_public_ip(a) for a in addresses)


def is_public_url(url: str) -> bool:
    try:
        parsed = httpx.URL(url)
    except Exception:
        return False
    return parsed.scheme in ("http", "https") and is_public_host(parsed.host)


def guard_request(request: httpx.Request) -> None:
    """httpx `request` event hook: raise before connecting anywhere internal."""
    if request.url.scheme not in ("http", "https") or not is_public_host(request.url.host):
        logger.warning("url_safety: refused to fetch %s (not a public address)",
                       request.url.host)
        raise UnsafeDestination(
            f"refused to fetch {request.url.host}: not a public address",
            request=request,
        )


EVENT_HOOKS = {"request": [guard_request]}
