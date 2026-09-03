"""
Which query gets the most out of Tsenta. Measures; stores nothing.

The extension sweep was built on the theory that their client pins
`autoApplyOnly: true`, so the postings it cannot auto-apply to are rendered to
nobody. Sweeping the API both ways from the browser returned exactly 214 rows
either way, which disproved it. That was worth knowing and it leaves the real
question open: **is 214 the whole board, or the whole board under one set of
filters we happened to copy?**

This asks the same question from the server, where the credential now lives, so
it can try a dozen variants instead of two and — the part the browser test could
not do — compare *which* postings each one returns. A variant with the same
total but different ids is a variant worth adding; one with a bigger total that
is a superset is a straight upgrade; one that matches the baseline exactly is
noise, and knowing that is worth as much.

    docker compose -f docker-compose.prod.yml run --rm web python scripts/probe_tsenta.py

Read-only in every sense: no job is stored, no counter moves, and the only
write anywhere is `linked_auth` recording that it minted a token.
"""

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# Deliberately gentle: this makes a few hundred requests across every variant,
# against a small company's API, for a question that is asked once.
PAUSE_SECONDS = 0.7
MAX_PAGES = 60

# The variants, and what each one is asking.
#
# `limit` is in the first few because the browser was capped at 20 however
# large a number it sent — but a browser request and a server request are not
# obviously the same to a rate limiter, and 100 per page would be a fifth of
# the round trips.
VARIANTS = [
    ("baseline (what the sweep sends)", {"locations": "country:US"}),
    ("bigger page",                     {"locations": "country:US", "limit": "100"}),
    ("no location filter",              {}),
    ("datePosted=all",                  {"locations": "country:US", "datePosted": "all"}),
    ("autoApplyOnly=true",              {"locations": "country:US", "autoApplyOnly": "true"}),
    ("autoApplyOnly=false",             {"locations": "country:US", "autoApplyOnly": "false"}),
    ("no filters at all",               {"datePosted": "all"}),
    ("remote anywhere",                 {"locations": "remote"}),
]

# Partitions, for the ceiling the first run found.
#
# Dropping `locations` or setting `datePosted=all` does not return more of one
# list — it returns a *different, larger* list, and both stop at page 21 with
# HTTP 400. Twenty pages of twenty is 400 rows, which is a hard offset cap, not
# the end of anything: the baseline's 215 ends on a short page, and these do
# not end at all.
#
# The way past a capped list is to ask for it in slices small enough to fit
# under the cap, and union what comes back. These are the candidate slice keys.
# A slice returning 400 is still truncated and needs slicing further; one
# returning fewer is complete.
PARTITIONS = [
    ("datePosted", ["all", "past_24_hours", "past_week", "past_month",
                    "past_3_days", "today"]),
    ("locations", ["country:US", "remote", "state:CA", "state:NY", "state:TX",
                   "state:WA", "state:MA"]),
    ("sortBy", ["relevance", "date", "newest", "recent"]),
]


def _ids(jobs):
    """What identifies a posting, for comparing one variant against another."""
    return {job.get("source_job_id") or job.get("url") for job in jobs}


def _sweep(client, headers, extra, api):
    from app.services.harvest import extract_jobs
    from app.services.sources.tsenta import PAGE_SIZE, SOURCE

    seen: set = set()
    pages = 0
    rows = 0
    served = 0
    stopped = "end of list"

    for page in range(1, MAX_PAGES + 1):
        params = {"limit": str(PAGE_SIZE), "page": str(page), **extra}
        try:
            response = client.get(api, params=params, headers=headers)
        except Exception as exc:
            stopped = f"request failed: {exc}"
            break
        if response.status_code != 200:
            stopped = f"HTTP {response.status_code}"
            break
        try:
            body = response.json()
        except Exception:
            stopped = "unreadable answer"
            break

        jobs = extract_jobs(body, source=SOURCE)
        if not jobs:
            stopped = "empty page"
            break

        pages += 1
        rows += len(jobs)
        seen |= _ids(jobs)
        if not served:
            served = len(jobs)
        if len(jobs) < served:
            stopped = "short page"
            break
        if page == MAX_PAGES:
            stopped = "page budget"
            break
        time.sleep(PAUSE_SECONDS)

    return {"pages": pages, "rows": rows, "served": served,
            "stopped": stopped, "ids": seen}


def main():
    import httpx

    from app.database import SessionLocal
    from app.services import linked_auth
    from app.services.sources.tsenta import API, SITE

    db = SessionLocal()
    try:
        token = linked_auth.id_token(db, SITE)
        if not token:
            row = linked_auth.get(db, SITE)
            sys.exit(
                "No usable credential. "
                + (f"Last error: {row.last_error}" if row is not None
                   else "Open the board in your browser once with the extension on.")
            )

        headers = {"accept": "application/json", "authorization": f"Bearer {token}"}
        client = httpx.Client(timeout=30, follow_redirects=True)
        baseline: set = set()
        try:
            print(f"{'variant':<34} {'pages':>6} {'rows':>6} {'served':>7} "
                  f"{'unique':>7} {'new':>6}  stopped")
            print("-" * 92)
            for label, extra in VARIANTS:
                out = _sweep(client, headers, extra, API)
                if not baseline:
                    baseline = set(out["ids"])
                    new = 0
                else:
                    new = len(out["ids"] - baseline)
                print(f"{label:<34} {out['pages']:>6} {out['rows']:>6} "
                      f"{out['served']:>7} {len(out['ids']):>7} {new:>6}  "
                      f"{out['stopped']}")
        finally:
            client.close()

        print()
        print("`served` is the page size the board actually gave, which is not")
        print("always the one asked for. `new` counts postings this variant")
        print("returned that the baseline did not — the only column that says")
        print("whether a variant is worth adding to the sweep.")

        # ---- Past the 400-row ceiling -------------------------------------
        #
        # A variant stopping at `HTTP 400` on page 21 has not reached the end
        # of anything; it has hit an offset cap. So the question stops being
        # "which single query is best" and becomes "which set of slices covers
        # the most between them".
        print()
        print("=" * 92)
        print("Slicing, to get under the 400-row ceiling")
        print("=" * 92)
        client = httpx.Client(timeout=30, follow_redirects=True)
        union: set = set()
        try:
            for key, values in PARTITIONS:
                print()
                print(f"--- {key} " + "-" * (86 - len(key)))
                print(f"{'value':<20} {'pages':>6} {'rows':>6} {'unique':>7} "
                      f"{'new to union':>13}  stopped")
                for value in values:
                    out = _sweep(client, headers, {key: value, "datePosted": "all"},
                                 API)
                    added = len(out["ids"] - union)
                    union |= out["ids"]
                    flag = "  <-- still truncated" if out["stopped"] == "HTTP 400" else ""
                    print(f"{value:<20} {out['pages']:>6} {out['rows']:>6} "
                          f"{len(out['ids']):>7} {added:>13}  "
                          f"{out['stopped']}{flag}")
        finally:
            client.close()

        print()
        print(f"Distinct postings across every slice: {len(union)}")
        print(f"The current sweep collects:           {len(baseline)}")
        print()
        print("A slice that stopped at HTTP 400 is still truncated and needs")
        print("slicing further. One that ended on a short or empty page is")
        print("complete, and is safe to sweep as it stands.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
