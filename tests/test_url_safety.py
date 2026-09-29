"""
A posting's URL must not be able to point the server at itself.

Link resolution, enrichment, liveness and the careers-site sniffer fetch URLs
that third parties wrote, and follow their redirects. Without this, a posting
linking to `http://redis:6379` or a metadata endpoint had its response stored
as the job's description.
"""

import httpx
import httpcore
import ssl
import pytest

from app.services import url_safety


@pytest.fixture
def resolves_to(monkeypatch):
    def _set(*addresses):
        monkeypatch.setattr(url_safety, "_resolve", lambda host: list(addresses))
    return _set


class TestWhatCountsAsPublic:
    @pytest.mark.parametrize("url", [
        "http://127.0.0.1/", "http://10.0.0.5/admin", "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/", "http://[::1]/",
        "http://localhost:8000/", "http://redis:6379/", "http://web:8000/auth/check",
        "http://printer.local/", "http://metadata.google.internal/",
        "http://224.0.0.1/", "http://[ff02::1]/", "http://[fec0::1]/",
        "file:///etc/passwd", "ftp://example.com/",
    ])
    def test_internal_destinations_are_refused(self, url):
        assert url_safety.is_public_url(url) is False

    def test_an_ordinary_site_is_allowed(self):
        assert url_safety.is_public_url("https://boards.greenhouse.io/acme/jobs/1") is True

    def test_a_public_name_that_resolves_inside_is_refused(self, resolves_to):
        resolves_to("10.1.2.3")
        assert url_safety.is_public_url("https://sneaky.example.com/") is False

    def test_one_private_answer_among_public_ones_is_enough_to_refuse(self, resolves_to):
        resolves_to("93.184.216.34", "127.0.0.1")
        assert url_safety.is_public_url("https://mixed.example.com/") is False


class TestTheHookStopsTheRequest:
    def _client(self, handler):
        return httpx.Client(transport=httpx.MockTransport(handler),
                            follow_redirects=True, event_hooks=url_safety.EVENT_HOOKS)

    def test_a_direct_internal_url_never_connects(self):
        seen = []
        with self._client(lambda r: seen.append(r) or httpx.Response(200)) as client:
            with pytest.raises(httpx.RequestError):
                client.get("http://redis:6379/")
        assert seen == []

    def test_a_redirect_into_the_network_is_stopped_at_the_hop(self):
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})

        with self._client(handler) as client:
            with pytest.raises(httpx.RequestError):
                client.get("https://jobs.example.com/apply")
        assert seen == ["https://jobs.example.com/apply"]

    def test_a_public_url_is_fetched(self):
        with self._client(lambda r: httpx.Response(200, text="ok")) as client:
            assert client.get("https://jobs.example.com/").text == "ok"


def test_dns_failures_are_closed_and_not_cached(monkeypatch):
    def unavailable(host):
        raise OSError("DNS is down")
    monkeypatch.setattr(url_safety, "_resolve", unavailable)
    assert url_safety.is_public_host("jobs.example.com") is False
    monkeypatch.setattr(url_safety, "_resolve", lambda host: ["93.184.216.34"])
    assert url_safety.is_public_host("jobs.example.com") is True
    monkeypatch.setattr(url_safety, "_resolve", lambda host: ["127.0.0.1"])
    assert url_safety.is_public_host("jobs.example.com") is False


def test_transport_pins_numeric_peer_and_preserves_tls_hostname(monkeypatch):
    connections, tls, writes, dns = [], [], [], []
    answers = iter([["93.184.216.34"], ["127.0.0.1"]])

    def resolve(host):
        dns.append(host)
        return next(answers)

    class Stream(httpcore.MockStream):
        def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            tls.append((server_hostname, ssl_context.check_hostname, ssl_context.verify_mode))
            return self

        def write(self, buffer, timeout=None):
            writes.append(buffer)
            return super().write(buffer, timeout=timeout)

    def connect(self, host, port, **kwargs):
        connections.append((host, port))
        return Stream([b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"])

    monkeypatch.setattr(url_safety, "_resolve", resolve)
    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    with url_safety.public_client() as client:
        assert client.get("https://jobs.example.com/").text == "ok"
        with pytest.raises(url_safety.UnsafeDestination):
            client.get("https://jobs.example.com/")
    assert connections == [("93.184.216.34", 443)]
    assert dns == ["jobs.example.com", "jobs.example.com"]
    assert tls == [("jobs.example.com", True, ssl.CERT_REQUIRED)]
    assert b"Host: jobs.example.com" in b"".join(writes)


def test_redirect_to_name_with_private_dns_never_connects(monkeypatch):
    connections = []
    monkeypatch.setattr(url_safety, "_resolve", lambda host: ["93.184.216.34"] if host == "jobs.example.com" else ["10.0.0.1"])

    def connect(self, host, port, **kwargs):
        connections.append(host)
        return httpcore.MockStream([b"HTTP/1.1 302 Found\r\nLocation: http://private.example.com/\r\nContent-Length: 0\r\n\r\n"])

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect)
    with url_safety.public_client(follow_redirects=True) as client:
        with pytest.raises(url_safety.UnsafeDestination):
            client.get("http://jobs.example.com/")
    assert connections == ["93.184.216.34"]


def test_failed_resolution_never_reaches_socket_backend(monkeypatch):
    def unavailable(host):
        raise OSError("DNS is down")
    def unexpected(*args, **kwargs):
        raise AssertionError("must not connect after failed resolution")
    monkeypatch.setattr(url_safety, "_resolve", unavailable)
    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", unexpected)
    with url_safety.public_client() as client:
        with pytest.raises(url_safety.UnsafeDestination, match="safely resolve"):
            client.get("https://jobs.example.com/")
