"""
Whether a stored posting is still up on the employer's side.

Thousands of applications sit prepared while their postings quietly close —
nothing in the pipeline ever looked at a job again after storing it, so "three
weeks old and long since filled" and "posted this morning" were shown with
identical confidence. This sweeps the jobs worth applying to (matched, docs
generated) and marks the ones that are gone, so a closed role is a visible
badge instead of a wasted application.

Deliberately conservative: only a hard 404/410, a page that says outright the
role is closed, or a known ATS bouncing the job URL back to its board index
counts. A timeout, a 403, a bot-check — anything ambiguous — just updates the
checked-at clock and leaves the job alone, because "we couldn't tell" wrongly
shown as "closed" would bury live jobs.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.models.job import Job, JobStatus

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

REQUEST_TIMEOUT = 12
# Only this much of the body is searched for a closed marker: the banner that
# says a job is gone is never megabytes in.
MAX_BODY_CHARS = 200_000

# Phrases that state the posting is finished. Every one of these was seen on a
# real closed-job page; vaguer wording stays out on purpose — a careers page
# that MENTIONS closing roles must not close this one.
CLOSED_MARKERS = (
    "no longer accepting applications",
    "this job is no longer available",
    "this position is no longer available",
    "job is no longer active",
    "position has been filled",
    "this position has been closed",
    "this job has been closed",
    "job you are looking for is no longer open",
    "this posting has expired",
    "job posting has expired",
    "this vacancy is now closed",
    "applications for this role are closed",
    "sorry, this job was removed",
    "job has expired",
)

# ATS hosts that answer a closed job by redirecting to the board index rather
# than 404ing. Only for these does "landed on a much shorter path" mean closed;
# on an arbitrary employer site the same redirect could be a plain URL change.
_REDIRECT_MEANS_CLOSED_HOSTS = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "smartrecruiters.com",
)


@dataclass
class LivenessResult:
    """What one check concluded."""
    state: str          # "open" | "closed" | "unknown"
    note: str = ""


def _host_matches(host: str, domains: tuple[str, ...]) -> bool:
    host = (host or "").lower()
    return any(host == d or host.endswith(f".{d}") for d in domains)


def check_url(url: str, client: httpx.Client) -> LivenessResult:
    """One posting URL, checked. Never raises."""
    try:
        response = client.get(url)
    except Exception as exc:
        return LivenessResult("unknown", f"unreachable: {exc}")

    if response.status_code in (404, 410):
        return LivenessResult("closed", f"HTTP {response.status_code}")
    if response.status_code >= 400:
        # 403/429/5xx say something about the server or about us, not about
        # the job. Ambiguity never closes a posting.
        return LivenessResult("unknown", f"HTTP {response.status_code}")

    final = str(response.url)
    if final != url:
        original_path = urlparse(url).path.rstrip("/")
        final_parsed = urlparse(final)
        if (
            _host_matches(final_parsed.hostname or "", _REDIRECT_MEANS_CLOSED_HOSTS)
            and original_path
            and len(final_parsed.path.rstrip("/")) < len(original_path) // 2
        ):
            return LivenessResult(
                "closed", "the ATS redirected the job URL back to the board index"
            )

    content_type = (response.headers.get("content-type") or "").lower()
    if "html" not in content_type and "json" not in content_type:
        return LivenessResult("open")

    marker = closed_marker(response.text)
    if marker:
        return LivenessResult("closed", f'the page says "{marker}"')
    return LivenessResult("open")


def closed_marker(html: str) -> str:
    """
    The phrase on this page that says the posting is finished, or "".

    Pulled out of `check_url` so the browser path can use it too. This checker
    reaches a page with `httpx`, which is exactly what LinkedIn and Dice refuse
    — so the postings most likely to be stale are the ones it can never look
    at. The browser opens them anyway, for enrichment, and was reading "this
    job is no longer available" as simply a page with no description in it:
    nothing learned, nothing closed, and the same dead URL opened again a week
    later.
    """
    body = (html or "")[:MAX_BODY_CHARS].lower()
    for marker in CLOSED_MARKERS:
        if marker in body:
            return marker
    return ""


def _check_target(job) -> str:
    """The URL worth checking: the employer's page over the aggregator's."""
    return (job.apply_url or job.url or "").strip()


def candidates(db, limit: int, recheck_days: int) -> list:
    """
    Jobs worth checking this pass: the ones a person might actually apply to,
    not yet known-closed, and not checked recently. Never-checked first, then
    the ones whose last check is oldest.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, recheck_days))
    return (
        db.query(Job)
        .filter(
            Job.status.in_([JobStatus.matched, JobStatus.docs_generated]),
            Job.closed_at.is_(None),
            (Job.liveness_checked_at.is_(None)) | (Job.liveness_checked_at < cutoff),
        )
        .order_by(Job.liveness_checked_at.asc().nullsfirst(), Job.fetched_at.desc())
        .limit(max(1, limit))
        .all()
    )


def sweep(db, limit: int | None = None, workers: int | None = None) -> dict:
    """
    Check one budget's worth of jobs and record what was learned.

    Returns {"checked": n, "closed": n, "still_open": n, "unknown": n} plus
    "coverage" — see `coverage` for what that answers and why it is not one of
    the counters.
    """
    limit = limit if limit is not None else settings.LIVENESS_MAX_PER_CYCLE
    workers = workers if workers is not None else settings.LIVENESS_WORKERS
    recheck_days = settings.LIVENESS_RECHECK_DAYS

    jobs = candidates(db, limit, recheck_days)
    counts = {"checked": 0, "closed": 0, "still_open": 0, "unknown": 0}
    if not jobs:
        # Reported on the empty run too, so the key is always there for a
        # caller to read rather than present only on the runs that did work.
        counts["coverage"] = coverage(db)
        return counts

    targets = [(job.id, _check_target(job)) for job in jobs]
    now = datetime.now(timezone.utc)

    # The network happens outside the ORM: check everything first, then write
    # the outcomes back in one pass, so no transaction spans a slow site.
    from app.services.url_safety import EVENT_HOOKS

    with httpx.Client(
        headers=_HEADERS, timeout=REQUEST_TIMEOUT,
        follow_redirects=True, max_redirects=10,
        event_hooks=EVENT_HOOKS,
    ) as client:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(targets)))) as pool:
            results = list(
                pool.map(
                    lambda t: (t[0], check_url(t[1], client) if t[1]
                               else LivenessResult("unknown", "no URL stored")),
                    targets,
                )
            )

    by_id = {job.id: job for job in jobs}
    for job_id, result in results:
        job = by_id[job_id]
        counts["checked"] += 1
        job.liveness_checked_at = now
        if result.state == "closed":
            job.closed_at = now
            job.closed_note = result.note[:300]
            counts["closed"] += 1
        elif result.state == "open":
            counts["still_open"] += 1
        else:
            counts["unknown"] += 1
    db.commit()

    if counts["closed"]:
        logger.info(
            "liveness: %d of %d checked postings are closed",
            counts["closed"], counts["checked"],
        )
    # Nested rather than merged into `counts`. The four outcome counters are
    # what this sweep did; coverage is whether the schedule can keep up, which
    # is a property of the configuration and true between runs as well. Folding
    # them together made "checked 2, closed 1" and "1,200 sustainable" the same
    # kind of number, and broke the test that says those four keys are the
    # whole result.
    counts["coverage"] = coverage(db)
    if counts["coverage"]["sustainable"] is False:
        # Said out loud, because the failure is silent otherwise. `candidates`
        # orders never-checked first, so past the budget the oldest matched
        # jobs simply stop being re-checked and keep showing a months-old
        # "still open" — which is the state this module exists to remove,
        # moved from "never checked" to "checked once".
        logger.warning(
            "liveness: %d jobs worth checking against a budget of %d a day "
            "(%d per sweep, every %dh) on a %d-day recheck — the oldest "
            "verdicts will go stale. Raise LIVENESS_MAX_PER_CYCLE or shorten "
            "LIVENESS_INTERVAL_HOURS.",
            counts["worth_checking"], counts["daily_budget"],
            settings.LIVENESS_MAX_PER_CYCLE, settings.LIVENESS_INTERVAL_HOURS,
            settings.LIVENESS_RECHECK_DAYS,
        )
    return counts


def coverage(db) -> dict:
    """
    Whether the configured budget can actually keep every verdict fresh.

    `LIVENESS_MAX_PER_CYCLE` checks per sweep, one sweep every
    `LIVENESS_INTERVAL_HOURS`, each verdict standing for
    `LIVENESS_RECHECK_DAYS` — multiply those out and you get the number of
    jobs this can sustain. At the defaults that is 200 x 2 x 3 = 1,200. Above
    it the arithmetic does not work and no error is raised; the oldest rows
    just stop being reached.

    A numerator with a denominator, which is the shape `docs/IMPROVING.md`
    says most of this system's numbers are missing.
    """
    from sqlalchemy import func

    per_day = (
        settings.LIVENESS_MAX_PER_CYCLE
        * max(1.0, 24.0 / max(1, settings.LIVENESS_INTERVAL_HOURS))
    )
    sustainable_population = per_day * max(1, settings.LIVENESS_RECHECK_DAYS)
    worth_checking = (
        db.query(func.count(Job.id))
        .filter(
            Job.status.in_([JobStatus.matched, JobStatus.docs_generated]),
            Job.closed_at.is_(None),
        )
        .scalar()
    ) or 0
    return {
        "worth_checking": int(worth_checking),
        "daily_budget": int(per_day),
        "sustains": int(sustainable_population),
        "sustainable": worth_checking <= sustainable_population,
    }
