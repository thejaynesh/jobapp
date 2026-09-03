"""
Tsenta's recommendations API, asked from the server instead of from a tab.

`extension/tsenta.js` does this from inside a Tsenta page, using the ID token
the site's own client publishes to extensions. It works, and it only happens
when a browser is open on that board — so the board is as current as the last
time somebody was browsing, which is not a schedule.

This is the same sweep with the credential coming from `linked_auth` instead of
from the page. Two things make that possible and both were measured rather than
assumed: the API answers a server request (401 without a token, which is a
server that was reached), and Google's `securetoken` endpoint mints ID tokens
from a stored refresh token.

The rows are read by `harvest.extract_jobs` rather than by a mapper written
here. That is deliberate: the shape-based reader is already the thing turning
these exact payloads into the jobs in the database, so a bespoke mapper would
be a second opinion about a question already answered — and one that would
drift the first time the board renamed a field.
"""

import logging
import time

import httpx

logger = logging.getLogger(__name__)

API = "https://api.autojobs.me/api/v1/jobs/recommendations"

SITE = "tsenta"
SOURCE = "tsenta_harvest"

# Measured, not guessed. The board caps the page size at 20 however large a
# `limit` is sent, and the whole recommendation set came back as 11 pages of
# it. The ceilings below are headroom over that, not a target.
PAGE_SIZE = 20
MAX_PAGES = 60
MAX_ROWS = 4000

# Their client sends `locations`, and omits `autoApplyOnly` and `datePosted`
# entirely when they are unset — so leaving the last two off is how their own
# query builder expresses "no filter".
#
# `autoApplyOnly` was expected to be the one that mattered: their feed pins it
# on, so the postings they cannot auto-apply to are never rendered. Sweeping
# both ways returned exactly 214 rows either way, so it hides nothing here.
# It stays omitted regardless — omitting a filter cannot return fewer rows —
# but the reason for this sweep is reaching the API at all, and that is worth
# writing down rather than leaving a comment claiming a benefit that was
# measured away.
LOCATIONS = "country:US"

PAUSE_SECONDS = 0.6
TIMEOUT_SECONDS = 30


def _page_url_params(page: int) -> dict:
    return {"limit": str(PAGE_SIZE), "page": str(page), "locations": LOCATIONS}


def sweep(db, *, client: httpx.Client | None = None) -> dict:
    """
    Page the board and store what comes back. Never raises.

    Returns the same shape the extension's sweep reports, so one panel can show
    both and a board that moved from one path to the other stays comparable.
    """
    from app.services import linked_auth
    from app.services.harvest import extract_jobs, save_harvested_jobs

    outcome = {"pages": 0, "rows": 0, "limit": 0, "stopped": "", "detail": "",
               "inserted": 0, "merged": 0, "skipped": 0, "invalid": 0}

    token = linked_auth.id_token(db, SITE)
    if not token:
        row = linked_auth.get(db, SITE)
        outcome["stopped"] = "not linked" if row is None else "credential refused"
        outcome["detail"] = (row.last_error or "") if row is not None else (
            "Open the board in your browser once with the extension on."
        )
        return outcome

    owns_client = client is None
    client = client or httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=True)
    headers = {"accept": "application/json", "authorization": f"Bearer {token}"}

    # The size the board actually serves, learned from page one. Comparing a
    # later page against the size *requested* is how a silently capped `limit`
    # reads as the end of the list — the mistake this sweep's browser-side twin
    # was written to avoid, and the same one applies here.
    served = 0
    try:
        for page in range(1, MAX_PAGES + 1):
            try:
                response = client.get(API, params=_page_url_params(page),
                                      headers=headers)
            except Exception as exc:
                outcome["stopped"] = "request failed"
                outcome["detail"] = str(exc)[:200]
                break

            if response.status_code in (401, 403):
                # The token was fine when it was minted and is not now. Drop it
                # so the next run mints a fresh one rather than reusing this.
                linked_auth.forget(SITE)
                outcome["stopped"] = "not signed in"
                outcome["detail"] = f"HTTP {response.status_code}"
                break
            if response.status_code != 200:
                outcome["stopped"] = f"HTTP {response.status_code}"
                break

            try:
                body = response.json()
            except Exception as exc:
                outcome["stopped"] = "unreadable answer"
                outcome["detail"] = str(exc)[:200]
                break

            jobs = extract_jobs(body, source=SOURCE)
            if not jobs:
                outcome["stopped"] = "empty page"
                break

            stored = save_harvested_jobs(db, jobs)
            for key in ("inserted", "merged", "skipped", "invalid"):
                outcome[key] += int(stored.get(key) or 0)

            outcome["pages"] += 1
            outcome["rows"] += len(jobs)
            if not served:
                served = len(jobs)

            if len(jobs) < served:
                outcome["stopped"] = "short page"
                break
            if outcome["rows"] >= MAX_ROWS:
                outcome["stopped"] = "row budget"
                break
            if page == MAX_PAGES:
                outcome["stopped"] = "page budget"
                break

            time.sleep(PAUSE_SECONDS)
    finally:
        if owns_client:
            client.close()

    outcome["limit"] = served or PAGE_SIZE
    outcome["stopped"] = outcome["stopped"] or "end of list"
    logger.info(
        "tsenta sweep: %d page(s), %d row(s), %d new, %d enriched — %s",
        outcome["pages"], outcome["rows"], outcome["inserted"], outcome["merged"],
        outcome["stopped"],
    )
    return outcome
