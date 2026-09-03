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
# `limit` is sent — from a browser and from a server alike — so the round-trip
# count is not negotiable and everything below is shaped around it.
PAGE_SIZE = 20

# Twenty pages is where the API starts answering HTTP 400. That is an offset
# cap at 400 rows, not the end of anything: a query that genuinely runs out
# ends on a short page well before it.
MAX_PAGES = 20
CAP_ROWS = MAX_PAGES * PAGE_SIZE

# Ceiling across every slice of one run, so a board that suddenly has ten times
# as much cannot turn one scheduled sweep into an afternoon of requests.
MAX_ROWS = 20_000

PAUSE_SECONDS = 0.6
TIMEOUT_SECONDS = 30


# What the probe found, and it is worth stating plainly because two of these
# are the opposite of what the code used to assume.
#
# `autoApplyOnly` does nothing. Their feed pins it on and the theory was that
# the postings they cannot auto-apply to are rendered to nobody; sweeping both
# ways returns an identical set. Omitted anyway — omitting a filter cannot
# return fewer rows — but it buys nothing.
#
# `datePosted` is a switch, not a window. Absent, the API returns 216 postings
# and ends properly. Present, with *any* value — `all`, `today`,
# `past_24_hours` — it returns a different and much larger list, and every
# value returns the same one. So it is not a date filter we can partition on;
# it is the difference between their recommendation feed and their whole index.
#
# `sortBy` does nothing either: four values, one identical set of 400.
#
# `locations` is the only real partition. Slicing by state, each slice fits
# under the 400 cap or comes close, and the union across a handful of states
# was 1,845 postings against the 216 this sweep used to collect.
WIDE = {"datePosted": "all"}

# The fifty states plus DC. Ordinary US postal codes, which is the vocabulary
# `locations` takes — `state:TX` was tried and works.
_STATES = (
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN",
    "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY",
)


def recommendation_slice() -> list[dict]:
    """
    Their feed: what the site itself would show. 216 postings, ends properly.

    Cheap — eleven round trips — so this is what the frequent sweep runs.
    """
    return [{"locations": "country:US"}]


def index_slices() -> list[dict]:
    """
    The whole index, cut into pieces that fit under the 400-row cap.

    One per state because that is the axis that actually partitions: the same
    query without it truncates at 400 and says nothing about how much is
    behind that. `remote` is swept without `datePosted`, because with it the
    board returns nothing and without it returns fifteen — which makes no sense
    from outside and is not worth theorising about when the measurement is
    right there.
    """
    slices = [{**WIDE, "locations": f"state:{code}"} for code in _STATES]
    slices.append({"locations": "remote"})
    return slices


def _page_params(page: int, query: dict) -> dict:
    return {"limit": str(PAGE_SIZE), "page": str(page), **query}


def _sweep_one(db, client, headers, query: dict, outcome: dict) -> str:
    """
    One slice, paged to its end or to the cap. Returns why it stopped.

    Stores as it goes rather than collecting first: a slice that fails on page
    twelve has still contributed eleven pages of real postings, and throwing
    those away to keep the function tidy would be throwing away jobs.
    """
    from app.services import linked_auth
    from app.services.harvest import extract_jobs, save_harvested_jobs

    served = 0
    for page in range(1, MAX_PAGES + 1):
        try:
            response = client.get(API, params=_page_params(page, query),
                                  headers=headers)
        except Exception as exc:
            outcome["detail"] = str(exc)[:200]
            return "request failed"

        if response.status_code in (401, 403):
            # Good when it was minted and not now. Drop it so the next run
            # mints a fresh one rather than reusing this for the hour.
            linked_auth.forget(SITE)
            outcome["detail"] = f"HTTP {response.status_code}"
            return "not signed in"
        if response.status_code == 400 and page > 1:
            # The offset cap. Not an error and not the end of the list — this
            # slice has more behind it than the API will hand over, and the
            # only way to reach it is a narrower slice.
            return "capped"
        if response.status_code != 200:
            outcome["detail"] = f"HTTP {response.status_code}"
            return f"HTTP {response.status_code}"

        try:
            body = response.json()
        except Exception as exc:
            outcome["detail"] = str(exc)[:200]
            return "unreadable answer"

        jobs = extract_jobs(body, source=SOURCE)
        if not jobs:
            return "empty page"

        stored = save_harvested_jobs(db, jobs)
        for key in ("inserted", "merged", "skipped", "invalid"):
            outcome[key] += int(stored.get(key) or 0)
        outcome["pages"] += 1
        outcome["rows"] += len(jobs)

        if not served:
            served = len(jobs)
            outcome["limit"] = outcome["limit"] or served
        # A page shorter than the one the board served first is the last page.
        # Against the served size, never the requested one: this API caps
        # `limit` at 20 silently, and comparing against what was asked for
        # reads that cap as the end of the list on page one.
        if len(jobs) < served:
            return "short page"
        if outcome["rows"] >= MAX_ROWS:
            return "row budget"
        if page == MAX_PAGES:
            return "capped"

        time.sleep(PAUSE_SECONDS)
    return "capped"


def sweep(db, *, client: httpx.Client | None = None,
          slices: list[dict] | None = None, deep: bool = False) -> dict:
    """
    Page the board and store what comes back. Never raises.

    Two modes, because the API has two lists behind one endpoint. Without
    `datePosted` it answers with their recommendation feed — 216 postings, and
    it ends properly — which is what the site itself shows and what is worth
    re-reading every few hours. With it, it answers from the whole index, which
    is far larger and which the offset cap will not let anyone page through in
    one query. `deep` sweeps that second list, cut into per-state slices that
    each fit under the cap.

    `capped_slices` in the result is the number that matters for coverage: a
    slice that stopped at the cap has more behind it that this partition cannot
    reach, and the fix is a narrower one rather than more pages.
    """
    from app.services import linked_auth

    outcome = {"pages": 0, "rows": 0, "limit": 0, "stopped": "", "detail": "",
               "inserted": 0, "merged": 0, "skipped": 0, "invalid": 0,
               "slices": 0, "capped_slices": 0, "deep": bool(deep)}

    token = linked_auth.id_token(db, SITE)
    if not token:
        row = linked_auth.get(db, SITE)
        outcome["stopped"] = "not linked" if row is None else "credential refused"
        outcome["detail"] = (row.last_error or "") if row is not None else (
            "Open the board in your browser once with the extension on."
        )
        return outcome

    if slices is None:
        slices = index_slices() if deep else recommendation_slice()

    owns_client = client is None
    client = client or httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=True)
    headers = {"accept": "application/json", "authorization": f"Bearer {token}"}

    # A slice that ran out of postings did its job. Anything else is news.
    ORDINARY = ("short page", "empty page", "capped", "row budget")

    capped: list[str] = []
    fatal = ""
    failed = ""
    # One re-mint, not a loop. An ID token lasts an hour and the deep sweep is
    # a thousand requests, so a run that starts against a token minted fifty
    # minutes ago by the feed sweep — same worker process, same cache — walks
    # off the end of it partway through. That is exactly what happened on the
    # first real run: thirty-three slices of fifty-one, then 401, and the last
    # eighteen states were never asked for.
    #
    # Once, because a second failure straight after a fresh mint is the
    # credential being refused rather than expiring, and retrying that is just
    # a faster way to be refused.
    reminted = False
    try:
        for query in slices:
            stopped = _sweep_one(db, client, headers, query, outcome)

            if stopped == "not signed in" and not reminted:
                reminted = True
                fresh = linked_auth.id_token(db, SITE)
                if fresh:
                    headers["authorization"] = f"Bearer {fresh}"
                    logger.info("tsenta sweep: token expired mid-run; re-minted")
                    # The same slice again: it stopped where the token died, and
                    # whatever it had already stored is deduplicated on the way
                    # back in rather than counted twice as new.
                    stopped = _sweep_one(db, client, headers, query, outcome)

            outcome["slices"] += 1
            if stopped == "capped":
                outcome["capped_slices"] += 1
                capped.append(query.get("locations") or "?")
            elif stopped not in ORDINARY:
                # Kept rather than dropped. Rolling a refused query up into
                # "end of list" is exactly how a broken slice hides inside a
                # sweep that otherwise looks healthy — and with fifty slices
                # there is a lot of healthy to hide in.
                failed = failed or stopped
            # A dead credential or an unreachable board will be dead for every
            # remaining slice too; fifty more of them is fifty more ways to say
            # the same thing.
            if stopped in ("not signed in", "request failed"):
                fatal = stopped
                break
            if outcome["rows"] >= MAX_ROWS:
                fatal = "row budget"
                break
    finally:
        if owns_client:
            client.close()

    outcome["limit"] = outcome["limit"] or PAGE_SIZE
    outcome["stopped"] = fatal or failed or (
        f"{outcome['capped_slices']} of {outcome['slices']} slices capped"
        if outcome["capped_slices"] else "end of list"
    )
    if capped:
        # Named, because these are the postings we know we are missing and the
        # only actionable thing about them is which slice hides them.
        outcome["detail"] = ("still capped: " + ", ".join(capped[:12]))[:200]

    logger.info(
        "tsenta sweep (%s): %d slice(s), %d page(s), %d row(s), %d new, "
        "%d enriched — %s",
        "index" if deep else "feed", outcome["slices"], outcome["pages"],
        outcome["rows"], outcome["inserted"], outcome["merged"], outcome["stopped"],
    )
    return outcome
