"""
The front door.

Every test here turns authentication back on — `conftest` disables it for the
rest of the suite, so this file is the only place the gate is actually exercised.
"""

import time

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.services import auth


PASSWORD = "correct-horse-battery-staple"
TOKEN = "agent-token-under-test"
SIGNING_KEY = "a-real-signing-key-not-the-placeholder"


@pytest.fixture
def secured(monkeypatch, db):
    """An app with authentication on and properly configured."""
    from app.main import app
    from app.database import get_db

    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    monkeypatch.setattr(settings, "APP_PASSWORD", PASSWORD)
    monkeypatch.setattr(settings, "AGENT_TOKEN", TOKEN)
    monkeypatch.setattr(settings, "SECRET_KEY", SIGNING_KEY)
    monkeypatch.setattr(settings, "SESSION_COOKIE_SECURE", False)  # TestClient speaks http
    auth.reset_throttle()

    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app, follow_redirects=False) as c:
        yield c
    app.dependency_overrides.clear()
    auth.reset_throttle()


def _login(client) -> None:
    response = client.post("/login", data={"password": PASSWORD, "next": "/apps"})
    assert response.status_code == 303


class TestBrowserGate:
    def test_anonymous_request_is_redirected_to_login(self, secured):
        response = secured.get("/jobs")
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")

    def test_the_original_destination_is_preserved(self, secured):
        response = secured.get("/jobs")
        assert "next=%2Fjobs" in response.headers["location"]

    def test_query_string_survives_the_round_trip(self, secured):
        response = secured.get("/jobs?status=matched")
        assert "status%3Dmatched" in response.headers["location"]

    def test_login_then_access(self, secured):
        _login(secured)
        assert secured.get("/jobs").status_code == 200

    def test_wrong_password_is_rejected(self, secured):
        response = secured.post("/login", data={"password": "wrong", "next": "/apps"})
        assert response.status_code == 401
        assert "Incorrect password" in response.text

    def test_logout_clears_the_session(self, secured):
        _login(secured)
        assert secured.get("/jobs").status_code == 200
        secured.post("/logout")
        assert secured.get("/jobs").status_code == 303

    def test_login_page_itself_is_reachable(self, secured):
        assert secured.get("/login").status_code == 200

    def test_static_assets_are_reachable(self, secured):
        # The login page is unstyled without them.
        response = secured.get("/static/css/main.css")
        assert response.status_code == 200

    def test_health_stays_public(self, secured):
        assert secured.get("/health").status_code == 200


class TestHtmxGate:
    """
    An HTMX request that follows a 303 pastes the login page into whatever
    fragment it was updating. It needs a header the client can act on instead.
    """

    def test_htmx_gets_hx_redirect_not_a_redirect(self, secured):
        response = secured.get("/jobs", headers={"HX-Request": "true"})
        assert response.status_code == 401
        assert response.headers["HX-Redirect"].startswith("/login")

    def test_htmx_body_is_not_the_login_page(self, secured):
        response = secured.get("/jobs", headers={"HX-Request": "true"})
        assert "<form" not in response.text


class TestOpenRedirect:
    """`?next=` is attacker-controllable, so it must only ever be a local path."""

    @pytest.mark.parametrize("hostile", [
        "https://evil.example.com/phish",
        "//evil.example.com",
        "http://evil.example.com",
    ])
    def test_offsite_next_is_ignored(self, secured, hostile):
        response = secured.post("/login", data={"password": PASSWORD, "next": hostile})
        assert response.status_code == 303
        assert response.headers["location"] == "/apps"

    def test_local_next_is_honoured(self, secured):
        response = secured.post("/login", data={"password": PASSWORD, "next": "/outreach"})
        assert response.headers["location"] == "/outreach"


class TestAgentToken:
    def test_agent_path_rejects_a_session_cookie(self, secured):
        # A logged-in browser is not an agent; the agent API wants its own token.
        _login(secured)
        response = secured.get("/api/agent/anything")
        assert response.status_code == 401

    def test_agent_path_rejects_a_wrong_token(self, secured):
        response = secured.get(
            "/api/agent/anything", headers={"Authorization": "Bearer nope"}
        )
        assert response.status_code == 401

    def test_agent_path_rejects_a_non_bearer_scheme(self, secured):
        response = secured.get(
            "/api/agent/anything", headers={"Authorization": f"Basic {TOKEN}"}
        )
        assert response.status_code == 401

    def test_a_valid_token_gets_past_the_gate(self, secured):
        # No agent routes exist yet, so passing the gate surfaces as a 404 from
        # the router rather than a 401 from the middleware.
        response = secured.get(
            "/api/agent/anything", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert response.status_code == 404

    def test_agent_api_is_closed_when_no_token_is_configured(self, secured, monkeypatch):
        monkeypatch.setattr(settings, "AGENT_TOKEN", "")
        response = secured.get(
            "/api/agent/anything", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert response.status_code == 503


class TestFailClosed:
    """
    Enabling authentication without configuring it must not read as "open".
    """

    def test_missing_password_serves_503(self, secured, monkeypatch):
        monkeypatch.setattr(settings, "APP_PASSWORD", "")
        response = secured.get("/jobs")
        assert response.status_code == 503
        assert "APP_PASSWORD" in response.json()["detail"]

    def test_placeholder_secret_key_serves_503(self, secured, monkeypatch):
        monkeypatch.setattr(settings, "SECRET_KEY", "change-me-in-production")
        response = secured.get("/jobs")
        assert response.status_code == 503
        assert "SECRET_KEY" in response.json()["detail"]

    def test_health_still_answers_while_misconfigured(self, secured, monkeypatch):
        # Otherwise the container is killed by its own health check and the
        # operator never sees the message explaining what to fix.
        monkeypatch.setattr(settings, "APP_PASSWORD", "")
        assert secured.get("/health").status_code == 200

    def test_disabling_auth_entirely_is_still_possible(self, secured, monkeypatch):
        monkeypatch.setattr(settings, "AUTH_ENABLED", False)
        assert secured.get("/jobs").status_code == 200


class TestSessionCookie:
    def test_cookie_is_httponly_and_samesite(self, secured):
        response = secured.post("/login", data={"password": PASSWORD, "next": "/apps"})
        header = response.headers["set-cookie"]
        assert "HttpOnly" in header
        assert "SameSite=lax" in header.lower().replace("samesite=lax", "SameSite=lax")

    def test_cookie_is_secure_by_default(self, monkeypatch):
        monkeypatch.delattr(settings, "SESSION_COOKIE_SECURE", raising=False)
        assert auth.cookie_secure() is True

    def test_secure_flag_can_be_turned_off_for_plain_http(self, monkeypatch):
        monkeypatch.setattr(settings, "SESSION_COOKIE_SECURE", False)
        assert auth.cookie_kwargs()["secure"] is False

    def test_turning_it_off_is_warned_about(self, monkeypatch):
        monkeypatch.setattr(settings, "AUTH_ENABLED", True)
        monkeypatch.setattr(settings, "SESSION_COOKIE_SECURE", False)
        assert "SESSION_COOKIE_SECURE=false" in auth.insecure_cookie_warning()

    def test_no_warning_when_secure(self, monkeypatch):
        monkeypatch.setattr(settings, "AUTH_ENABLED", True)
        monkeypatch.setattr(settings, "SESSION_COOKIE_SECURE", True)
        assert auth.insecure_cookie_warning() is None

    def test_debug_no_longer_affects_cookie_security(self, monkeypatch):
        # The two used to share a switch; a deployment must not lose cookie
        # protection just because someone turned on debugging.
        monkeypatch.setattr(settings, "DEBUG", True)
        monkeypatch.setattr(settings, "SESSION_COOKIE_SECURE", True)
        assert auth.cookie_kwargs()["secure"] is True

    def test_a_tampered_signature_is_rejected(self, monkeypatch):
        monkeypatch.setattr(settings, "SECRET_KEY", SIGNING_KEY)
        cookie = auth.issue_session()
        payload, _, _ = cookie.partition(".")
        assert not auth.session_valid(f"{payload}.forged")

    def test_a_cookie_signed_with_another_key_is_rejected(self, monkeypatch):
        monkeypatch.setattr(settings, "SECRET_KEY", "attacker-key")
        forged = auth.issue_session()
        monkeypatch.setattr(settings, "SECRET_KEY", SIGNING_KEY)
        assert not auth.session_valid(forged)

    def test_an_expired_session_is_rejected(self, monkeypatch):
        monkeypatch.setattr(settings, "SECRET_KEY", SIGNING_KEY)
        monkeypatch.setattr(settings, "SESSION_MAX_AGE_SECONDS", 1)
        cookie = auth.issue_session()
        assert auth.session_valid(cookie)
        later = time.time() + 10  # captured before patching, or the stub recurses
        with monkeypatch.context() as m:
            m.setattr(time, "time", lambda: later)
            assert not auth.session_valid(cookie)

    def test_a_future_dated_cookie_is_rejected(self, monkeypatch):
        monkeypatch.setattr(settings, "SECRET_KEY", SIGNING_KEY)
        payload = str(int(time.time()) + 9999)
        assert not auth.session_valid(f"{payload}.{auth._sign(payload)}")

    @pytest.mark.parametrize("junk", ["", "garbage", "no-dot", ".", "abc.def"])
    def test_malformed_cookies_are_rejected(self, monkeypatch, junk):
        monkeypatch.setattr(settings, "SECRET_KEY", SIGNING_KEY)
        assert not auth.session_valid(junk)


class TestLoginThrottle:
    def test_repeated_failures_lock_the_form(self, secured):
        for _ in range(auth.MAX_ATTEMPTS):
            secured.post("/login", data={"password": "wrong", "next": "/apps"})
        response = secured.post("/login", data={"password": PASSWORD, "next": "/apps"})
        assert response.status_code == 429
        assert "Too many attempts" in response.text

    def test_a_success_clears_the_counter(self, secured):
        for _ in range(auth.MAX_ATTEMPTS - 1):
            secured.post("/login", data={"password": "wrong", "next": "/apps"})
        _login(secured)
        for _ in range(auth.MAX_ATTEMPTS - 1):
            secured.post("/login", data={"password": "wrong", "next": "/apps"})
        # Still under the limit because the successful login reset the count.
        assert secured.post(
            "/login", data={"password": PASSWORD, "next": "/apps"}
        ).status_code == 303


class TestPasswordComparison:
    def test_empty_password_never_matches(self, monkeypatch):
        monkeypatch.setattr(settings, "APP_PASSWORD", "")
        assert auth.verify_password("") is False
        assert auth.verify_password("anything") is False

    def test_empty_agent_token_never_matches(self, monkeypatch):
        monkeypatch.setattr(settings, "AGENT_TOKEN", "")
        assert auth.verify_agent_token("") is False

    def test_correct_password_matches(self, monkeypatch):
        monkeypatch.setattr(settings, "APP_PASSWORD", PASSWORD)
        assert auth.verify_password(PASSWORD) is True

    @pytest.mark.parametrize("header,expected", [
        ("Bearer abc", "abc"),
        ("bearer abc", "abc"),
        ("Basic abc", ""),
        ("abc", ""),
        (None, ""),
        ("Bearer  spaced ", "spaced"),
    ])
    def test_bearer_parsing(self, header, expected):
        assert auth.bearer_token(header) == expected


class TestTheProxyCanAskWhetherThereIsASession:
    """
    Caddy serves `/storage/*` straight off the shared volume, so those requests
    never reached FastAPI and `require_authentication` — middleware precisely
    so that nobody has to remember to protect a route — never ran. What is in
    there is a tailored resume: full name, address, phone, complete work
    history. The UUID in the path made it unguessable, not private.

    `forward_auth` in the Caddyfile asks here first.
    """

    def test_a_valid_session_is_allowed(self, client, monkeypatch):
        monkeypatch.setattr(settings, "AUTH_ENABLED", True)
        client.cookies.set(auth.SESSION_COOKIE, auth.issue_session())
        response = client.get("/auth/check")
        assert response.status_code == 204

    def test_no_session_is_refused(self, client, monkeypatch):
        monkeypatch.setattr(settings, "AUTH_ENABLED", True)
        client.cookies.clear()
        assert client.get("/auth/check").status_code == 401

    def test_a_forged_session_is_refused(self, client, monkeypatch):
        monkeypatch.setattr(settings, "AUTH_ENABLED", True)
        client.cookies.set(auth.SESSION_COOKIE, "9999999999.notavalidsignature")
        assert client.get("/auth/check").status_code == 401

    def test_it_answers_rather_than_redirecting(self, client, monkeypatch):
        """
        A download is not a navigation. A 303 to the login form would be saved
        to disk as the PDF the user asked for.
        """
        monkeypatch.setattr(settings, "AUTH_ENABLED", True)
        client.cookies.clear()
        response = client.get("/auth/check", follow_redirects=False)
        assert response.status_code == 401
        assert not response.content


class TestOnlyTheHopWeAddedIsTrusted:
    """
    `X-Forwarded-For` is client-supplied, and Caddy *appends* the peer address
    rather than replacing it. Reading the first entry returned whatever the
    caller typed, so rotating the header meant the lockout never engaged — on a
    form whose whole threat model is one password and no user database.
    """

    def _client_for(self, header):
        from app.routers.auth import _client

        request = type("R", (), {
            "headers": {"X-Forwarded-For": header} if header else {},
            "client": type("C", (), {"host": "10.0.0.9"})(),
        })()
        return _client(request)

    def test_the_last_hop_wins(self):
        assert self._client_for("1.2.3.4, 203.0.113.7") == "203.0.113.7"

    def test_a_spoofed_single_value_does_not_become_the_identity(self):
        # Caddy would have appended the real address; a header with only the
        # attacker's value means no proxy touched it.
        assert self._client_for("1.2.3.4") == "1.2.3.4"

    def test_rotating_the_header_no_longer_resets_the_throttle(self):
        """The point of the fix: many spoofed values, one throttle bucket."""
        seen = {self._client_for(f"10.0.0.{n}, 203.0.113.7") for n in range(20)}
        assert seen == {"203.0.113.7"}

    def test_no_header_falls_back_to_the_peer(self):
        assert self._client_for(None) == "10.0.0.9"
