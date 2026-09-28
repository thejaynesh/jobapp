"""
Company boards from Common Crawl's URL index.

Every other way this system finds a board starts from something it already
has: a posting that links to one, a careers page it sniffed, a community list.
So a company whose postings never reach an aggregator we read, and that no list
mentions, is never found — and that is most companies.

Common Crawl publishes an index of every URL its monthly crawl saved, and ATS
boards live on a handful of hosts (`job-boards.greenhouse.io/<slug>`,
`jobs.ashbyhq.com/<slug>`, `<tenant>.wd5.myworkdayjobs.com/<site>`…). Walking
the index for those hosts lists the boards the crawl saw, whether or not any
posting of theirs ever reached us. An open-source aggregator found ~95,000
company boards this way; the first 3,000 index rows for one Greenhouse host held
212 distinct boards (measured 2026-09-28).

How it runs:

* Per crawl, per target host, page by page (a page is ~15,000 URLs), a
  budget of pages per run, resuming where the last run stopped. A new crawl
  starts the walk again, which is how boards created since are picked up.
* Every URL goes through `ats_discovery.extract_slugs` — the same patterns
  that read posting links — so a board here is exactly what discovery would
  have made of a link to it.
* Boards go to the registry as `commoncrawl`, which probes each one before
  polling it. Nothing found here is polled on the index's word alone.
* The index servers are shared and sometimes reset connections; a page that
  fails three times ends the run with its cursor intact.

Lever is absent on purpose: `jobs.lever.co` blocks Common Crawl's crawler in
its robots.txt, so the index has nothing for it.
"""

import json
import logging
import time
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

COLLINFO_URL = "https://index.commoncrawl.org/collinfo.json"
STATE_KEY = "commoncrawl_discovery"
_TIMEOUT = 120
_RETRIES = 3
_PAUSE_SECONDS = 1.0   # between index requests: the servers are shared

# (name, CDX query) per target. `matchType=domain` covers every subdomain,
# which is how tenant-per-subdomain ATSes (Workday, BambooHR…) are listed.
TARGETS: list[tuple[str, str]] = [
    ("greenhouse-new", "url=job-boards.greenhouse.io/*"),
    ("greenhouse", "url=boards.greenhouse.io/*"),
    ("greenhouse-eu", "url=job-boards.eu.greenhouse.io/*"),
    ("ashby", "url=jobs.ashbyhq.com/*"),
    ("smartrecruiters", "url=jobs.smartrecruiters.com/*"),
    ("workable", "url=apply.workable.com/*"),
    ("workday", "url=myworkdayjobs.com&matchType=domain"),
    ("workday-shared", "url=myworkdaysite.com&matchType=domain"),
    ("oracle", "url=oraclecloud.com&matchType=domain&filter=~url:.*CandidateExperience.*"),
    ("eightfold", "url=eightfold.ai&matchType=domain"),
    ("bamboohr", "url=bamboohr.com&matchType=domain"),
    ("recruitee", "url=recruitee.com&matchType=domain"),
    ("teamtailor", "url=teamtailor.com&matchType=domain"),
    ("icims", "url=icims.com&matchType=domain"),
    ("personio", "url=jobs.personio.de&matchType=domain"),
    ("jibe", "url=jibeapply.com&matchType=domain"),
    ("rippling", "url=ats.rippling.com/*"),
    ("pinpoint", "url=pinpointhq.com&matchType=domain"),
]


def _get(url: str) -> httpx.Response:
    """GET with retries for the index's intermittent connection resets."""
    last: Exception | None = None
    for attempt in range(_RETRIES):
        try:
            resp = httpx.get(url, timeout=_TIMEOUT, follow_redirects=True)
            if resp.status_code in (429, 503):
                raise httpx.HTTPStatusError("index busy", request=resp.request,
                                            response=resp)
            resp.raise_for_status()
            return resp
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            last = exc
            time.sleep(_PAUSE_SECONDS * (2 ** attempt))
    raise last  # type: ignore[misc]


def latest_crawl() -> tuple[str, str]:
    """(crawl id, CDX endpoint) of the newest crawl."""
    crawls = _get(COLLINFO_URL).json()
    newest = crawls[0]
    return newest["id"], newest["cdx-api"]


def page_count(api: str, query: str) -> int:
    data = _get(f"{api}?{query}&showNumPages=true").json()
    return int(data.get("pages") or 0)


def page_urls(api: str, query: str, page: int) -> list[str]:
    """Every URL on one index page."""
    resp = _get(f"{api}?{query}&output=json&fl=url&page={page}")
    urls = []
    for line in resp.text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            url = json.loads(line).get("url")
        except (ValueError, AttributeError):
            continue
        if url:
            urls.append(url)
    return urls


def boards_in(urls: list[str]) -> dict[str, set[str]]:
    """The ATS boards a batch of URLs names, via discovery's own patterns."""
    from app.services.ats_discovery import extract_slugs

    found: dict[str, set[str]] = {}
    # Many URLs per board (one per posting), so read each distinct prefix once.
    for url in dict.fromkeys(u.split("?", 1)[0] for u in urls):
        for ats, slugs in extract_slugs(url).items():
            found.setdefault(ats, set()).update(slugs)
    return found


def due(state: dict, interval_hours: float) -> bool:
    last = state.get("last_run")
    if not last:
        return True
    try:
        then = datetime.fromisoformat(last)
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - then).total_seconds() >= interval_hours * 3600 - 300


def run(db, pages_per_run: int = 20, force: bool = False,
        interval_hours: float = 24) -> dict:
    """
    One budgeted step of the walk. Returns what it did.

    State lives on the profile under `STATE_KEY`: the crawl being walked, and
    per target the page count and the next page to read.
    """
    from app.models.profile import Profile
    from app.services import company_boards

    profile = db.query(Profile).first()
    if profile is None:
        return {"ok": False, "detail": "no profile"}
    state = dict((profile.data or {}).get(STATE_KEY) or {})
    if not force and not due(state, interval_hours):
        return {"ok": True, "skipped": True, "detail": "not due"}

    report = {"ok": True, "pages": 0, "boards_seen": 0, "new_boards": 0, "targets": []}
    try:
        crawl, api = latest_crawl()
    except Exception as exc:
        logger.warning("commoncrawl: index list unavailable: %s", exc)
        return {"ok": False, "detail": f"index list unavailable: {exc}"}

    if state.get("crawl") != crawl:
        # A new crawl: walk again from the start, which is how boards created
        # since the last one are found.
        state = {"crawl": crawl, "api": api, "pages": {}, "cursor": {}}
    pages = dict(state.get("pages") or {})
    cursor = dict(state.get("cursor") or {})

    budget = max(0, int(pages_per_run))
    found: dict[str, set[str]] = {}
    stopped = None
    for name, query in TARGETS:
        if budget <= 0:
            break
        try:
            if name not in pages:
                pages[name] = page_count(api, query)
                time.sleep(_PAUSE_SECONDS)
            while budget > 0 and cursor.get(name, 0) < pages[name]:
                page = cursor.get(name, 0)
                urls = page_urls(api, query, page)
                for ats, slugs in boards_in(urls).items():
                    found.setdefault(ats, set()).update(slugs)
                cursor[name] = page + 1
                budget -= 1
                report["pages"] += 1
                time.sleep(_PAUSE_SECONDS)
        except Exception as exc:
            # Keep the cursor where it is; the next run resumes this page.
            stopped = f"{name}: {exc}"
            logger.warning("commoncrawl: stopped at %s", stopped)
            break
        report["targets"].append({"target": name, "page": cursor.get(name, 0),
                                  "of": pages.get(name, 0)})

    if found:
        report["boards_seen"] = sum(len(v) for v in found.values())
        report["new_boards"] = company_boards.record_boards(
            db, found, origin="commoncrawl", revive=False)
    remaining = sum(max(0, pages.get(n, 1) - cursor.get(n, 0)) for n, _ in TARGETS)

    import copy

    # Re-read: a run takes minutes, and the agent poll, a fetch cycle and a
    # settings save all write this blob meanwhile — writing the copy taken at
    # the start would revert them. Only this key is ours.
    db.refresh(profile)
    fresh = profile
    data = copy.deepcopy(fresh.data or {})
    data[STATE_KEY] = {
        "crawl": crawl, "api": api, "pages": pages, "cursor": cursor,
        "last_run": datetime.now(timezone.utc).isoformat(),
        "last_report": {k: v for k, v in report.items() if k != "targets"},
        "complete": remaining == 0 and not stopped,
        **({"stopped": stopped[:300]} if stopped else {}),
    }
    fresh.data = data
    db.commit()
    if stopped:
        report["stopped"] = stopped
    report["complete"] = data[STATE_KEY]["complete"]
    logger.info("commoncrawl: %d pages, %d boards seen, %d new (crawl %s)",
                report["pages"], report["boards_seen"], report["new_boards"], crawl)
    return report
