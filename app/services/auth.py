"""
Who is allowed to talk to this application.

Two callers, two mechanisms, deliberately separate:

  browser  — a person, navigating. Password once, then a signed session cookie.
  agent    — the browser extension and the local agent, calling /api/agent/*.
             A bearer token, because there is nobody to type a password.

They are separate credentials so that revoking the agent's access does not lock
the user out of their own dashboard, and so a token leaked from an unpacked
extension cannot be replayed as a login.

Neither is a user database. This is a single-tenant self-hosted application; the
password and the token live in the environment alongside SMTP_PASSWORD and the
API keys, and the whole surface is "is this the one person who owns this
deployment, or their agent".

Fail closed
-----------
`AUTH_ENABLED` defaults to True, and enabling it without configuring it does not
quietly fall back to open access — `misconfiguration()` reports why, and the
application serves 503 to everything but /health until it is fixed. A signing
key left at its shipped placeholder counts as misconfigured: sessions signed
with a publicly known key are forgeable, which is indistinguishable from having
no authentication at all.
"""

import base64
import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass, field

from app.config import settings

logger = logging.getLogger(__name__)

SESSION_COOKIE = "jobapp_session"

# The value shipped in .env.example. Signing with it is signing with a key that
# is published in the repository.
PLACEHOLDER_SECRETS = frozenset({
    "change-me-in-production", "changeme", "secret", "", "your_secret_here",
})


def _secret() -> bytes:
    return (settings.SECRET_KEY or "").encode()


# ---------------------------------------------------------------------------
# Configuration state
# ---------------------------------------------------------------------------

def auth_enabled() -> bool:
    return bool(getattr(settings, "AUTH_ENABLED", True))


def misconfiguration() -> str | None:
    """
    Why authentication cannot be enforced, or None when it can.

    Phrased for whoever has to fix it, because it is rendered to them: this is
    the message on the 503 that a misconfigured deployment serves.
    """
    if not auth_enabled():
        return None
    if not (settings.APP_PASSWORD or "").strip():
        return (
            "APP_PASSWORD is not set. Set it to a long random string to log in, "
            "or set AUTH_ENABLED=false if this deployment is not reachable from "
            "the internet."
        )
    if (settings.SECRET_KEY or "").strip() in PLACEHOLDER_SECRETS:
        return (
            "SECRET_KEY is still the example value. Session cookies are signed "
            "with it, so a known key means anyone can forge a login. Generate "
            "one with: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )
    return None


def agent_auth_configured() -> bool:
    """Whether /api/agent/* can be served at all."""
    return bool((settings.AGENT_TOKEN or "").strip())


# ---------------------------------------------------------------------------
# Password
# ---------------------------------------------------------------------------

def verify_password(candidate: str) -> bool:
    """Constant-time check against the configured password."""
    expected = (settings.APP_PASSWORD or "").strip()
    if not expected:
        return False
    return hmac.compare_digest(candidate.encode(), expected.encode())


def verify_agent_token(candidate: str) -> bool:
    expected = (settings.AGENT_TOKEN or "").strip()
    if not expected:
        return False
    return hmac.compare_digest(candidate.encode(), expected.encode())


def bearer_token(header_value: str | None) -> str:
    """The token out of an `Authorization: Bearer <token>` header, or ""."""
    if not header_value:
        return ""
    scheme, _, token = header_value.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return token.strip()


# ---------------------------------------------------------------------------
# Session cookie
# ---------------------------------------------------------------------------
#
# `<issued-at>.<hmac>`, signed with SECRET_KEY. There is no session store: with
# one user there is nothing to look up, and the expiry rides in the signed
# payload so a stolen cookie cannot outlive it by being replayed later.

def _sign(payload: str) -> str:
    digest = hmac.new(_secret(), payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def issue_session() -> str:
    payload = str(int(time.time()))
    return f"{payload}.{_sign(payload)}"


def session_valid(cookie: str | None) -> bool:
    if not cookie:
        return False
    payload, _, signature = cookie.partition(".")
    if not payload or not signature:
        return False
    if not hmac.compare_digest(signature, _sign(payload)):
        return False
    try:
        issued_at = int(payload)
    except ValueError:
        return False
    age = time.time() - issued_at
    # A future timestamp means a forged or clock-skewed cookie; either way it is
    # not one we issued in a state we can reason about.
    if age < -60:
        return False
    return age <= session_max_age()


def session_max_age() -> int:
    return int(getattr(settings, "SESSION_MAX_AGE_SECONDS", 60 * 60 * 24 * 14))


def cookie_secure() -> bool:
    """
    Whether the session cookie is restricted to HTTPS.

    Its own setting rather than a reading of DEBUG: "am I debugging" and "is
    this connection encrypted" are unrelated questions, and tying them together
    means a deployment silently loses cookie protection the day DEBUG acquires
    any other meaning.
    """
    return bool(getattr(settings, "SESSION_COOKIE_SECURE", True))


def insecure_cookie_warning() -> str | None:
    """Why the session cookie is travelling in the clear, when it is."""
    if auth_enabled() and not cookie_secure():
        return (
            "SESSION_COOKIE_SECURE=false — the login cookie is sent over plain "
            "HTTP and anyone able to observe the connection can copy it and "
            "become you. Acceptable only until TLS is in front of this app."
        )
    return None


def cookie_kwargs() -> dict:
    """Cookie flags for `Response.set_cookie`."""
    return {
        "httponly": True,
        "samesite": "lax",
        "secure": cookie_secure(),
        "max_age": session_max_age(),
        "path": "/",
    }


# ---------------------------------------------------------------------------
# Login throttle
# ---------------------------------------------------------------------------
#
# One password and no user database means an unthrottled login form is a plain
# offline-speed guessing target. In-memory is the right scope: state that dies
# with the process is fine when the attack it stops is measured in seconds.

MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 300


@dataclass
class _Attempts:
    count: int = 0
    locked_until: float = 0.0


_attempts: dict[str, _Attempts] = {}


def throttle_state(client: str) -> _Attempts:
    return _attempts.setdefault(client, _Attempts())


def locked_out(client: str) -> int:
    """Seconds remaining on a lockout, or 0."""
    state = throttle_state(client)
    remaining = state.locked_until - time.time()
    return int(remaining) if remaining > 0 else 0


def record_failure(client: str) -> None:
    state = throttle_state(client)
    state.count += 1
    if state.count >= MAX_ATTEMPTS:
        state.locked_until = time.time() + LOCKOUT_SECONDS
        state.count = 0
        logger.warning("login: locking out %s for %ds", client, LOCKOUT_SECONDS)


def record_success(client: str) -> None:
    _attempts.pop(client, None)


def reset_throttle() -> None:
    """For tests, and for an operator who has locked themselves out."""
    _attempts.clear()


def generate_secret(length: int = 48) -> str:
    return secrets.token_urlsafe(length)
