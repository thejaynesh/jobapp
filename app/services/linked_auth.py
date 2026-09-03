"""
Minting a fresh ID token from a stored refresh token.

Firebase's web SDK signs a user in once and then keeps two things: an ID token
that lasts an hour, and a refresh token that lasts until it is revoked. Every
API call the site makes carries the first; the second exists only to produce
more of them, against a public Google endpoint that takes the project's web API
key — which is not a secret and is already sitting in the site's JavaScript
bundle.

That is the whole mechanism. A server holding the refresh token can do exactly
what the browser tab does, which is what makes a scheduled sweep possible at
all.

Three things this is careful about:

* **Rotation.** Google may hand back a *new* refresh token with each mint, and
  the old one may then stop working. Every response's `refresh_token` is stored
  before the ID token is returned, so a rotation cannot be lost by a later
  failure in the caller.
* **Caching.** An ID token is good for an hour and a sweep is a minute, so
  minting once per sweep is right and minting once per page would be forty
  round trips to Google for no reason. Cached in memory with a margin, because
  a worker process outlives a sweep and rarely outlives the hour.
* **Saying why it stopped.** A refresh token dies when the user signs out
  everywhere, changes their password, or the project revokes it. That is not a
  bug and not something the server can fix — the fix is to open the board in
  the browser once, which re-links automatically. So the failure is recorded on
  the row rather than raised into a stack trace nobody reads.
"""

import logging
import time
from datetime import datetime, timezone

import httpx

from app.models.linked_account import LinkedAccount

logger = logging.getLogger(__name__)

TOKEN_URL = "https://securetoken.googleapis.com/v1/token"

# An ID token lasts an hour. Five minutes of margin covers a sweep that starts
# just before the boundary and a clock that disagrees slightly with Google's.
_MARGIN_SECONDS = 300

TIMEOUT_SECONDS = 20

# site -> (id_token, expires_at_epoch). Process-local: a worker runs one sweep
# at a time and a cache miss costs one cheap request, so there is nothing here
# worth the complexity of sharing between processes.
_cache: dict[str, tuple[str, float]] = {}


def get(db, site: str) -> LinkedAccount | None:
    return db.get(LinkedAccount, site)


def link(db, site: str, api_key: str, refresh_token: str) -> LinkedAccount:
    """
    Store, or replace, the credential for a site.

    Called every time the extension sees the board, which is deliberate: a
    credential that has gone stale is repaired by the user visiting the site
    once, without anybody having to know that is the fix.
    """
    now = datetime.now(timezone.utc)
    row = db.get(LinkedAccount, site)
    if row is None:
        row = LinkedAccount(site=site, api_key=api_key, refresh_token=refresh_token,
                            linked_at=now)
        db.add(row)
    else:
        rotated = row.refresh_token != refresh_token
        row.api_key = api_key
        row.refresh_token = refresh_token
        row.linked_at = now
        if rotated:
            # A different token than the one that was failing is a fresh start;
            # keeping the old error would leave the panel red on a link that
            # has not been tried yet.
            row.last_error = None
            row.last_error_at = None
    db.commit()
    _cache.pop(site, None)
    return row


def _note_failure(db, row: LinkedAccount, message: str) -> None:
    row.last_error = message[:400]
    row.last_error_at = datetime.now(timezone.utc)
    db.commit()


def id_token(db, site: str) -> str | None:
    """
    A usable ID token for `site`, or None with the reason recorded.

    None has three meanings and the row says which: never linked, the mint was
    refused, or the mint could not be reached. Only the second is permanent,
    and even that one is repaired by opening the board in a browser.
    """
    cached = _cache.get(site)
    if cached and cached[1] > time.time():
        return cached[0]

    row = get(db, site)
    if row is None:
        logger.info("linked_auth: %s has never been linked", site)
        return None

    try:
        response = httpx.post(
            TOKEN_URL,
            params={"key": row.api_key},
            data={"grant_type": "refresh_token", "refresh_token": row.refresh_token},
            timeout=TIMEOUT_SECONDS,
        )
    except Exception as exc:
        _note_failure(db, row, f"could not reach the token service: {exc}")
        logger.warning("linked_auth: %s mint unreachable: %s", site, exc)
        return None

    if response.status_code != 200:
        # Google's body names the reason — TOKEN_EXPIRED, USER_DISABLED,
        # INVALID_REFRESH_TOKEN — and that distinction is the whole difference
        # between "sign in again" and "something is broken on our side".
        detail = ""
        try:
            detail = str((response.json().get("error") or {}).get("message") or "")
        except Exception:
            detail = response.text[:200]
        _note_failure(db, row, f"HTTP {response.status_code}: {detail}"
                               or f"HTTP {response.status_code}")
        logger.warning("linked_auth: %s refused the refresh token (%s): %s",
                       site, response.status_code, detail)
        return None

    try:
        body = response.json()
    except Exception as exc:
        _note_failure(db, row, f"unreadable reply: {exc}")
        return None

    token = str(body.get("id_token") or body.get("access_token") or "")
    if not token:
        _note_failure(db, row, "the reply carried no id_token")
        return None

    # Before returning, and before anything else can fail: a rotated token that
    # is not stored is a credential lost on the next call.
    rotated = str(body.get("refresh_token") or "")
    if rotated and rotated != row.refresh_token:
        row.refresh_token = rotated

    row.last_minted_at = datetime.now(timezone.utc)
    row.last_error = None
    row.last_error_at = None
    db.commit()

    try:
        lifetime = max(60, int(body.get("expires_in") or 3600))
    except (TypeError, ValueError):
        lifetime = 3600
    _cache[site] = (token, time.time() + lifetime - _MARGIN_SECONDS)
    return token


def forget(site: str) -> None:
    """Drop the cached token, so the next call mints a new one."""
    _cache.pop(site, None)
