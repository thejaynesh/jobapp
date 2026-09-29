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
    # Paylocity's page for a closed posting, which it redirects to.
    "that job does not exist or is not currently active",
    # UKG renders a closed opportunity's page as before, and says so in its data.
    '"opportunityisclosed":true',
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
        # Avature, on its own hosts and employers' alike, sends a posting it
        # no longer has to the portal's error page: /careers/JobDetail/… →
        # /careers/Error.
        if "/JobDetail/" in original_path and final_parsed.path.rstrip("/").endswith("/Error"):
            return LivenessResult("closed", "the portal redirected the posting to its error page")
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
    expired = stated_expiry(final, response.text)
    if expired:
        return LivenessResult("closed", f"the posting expired on {expired:%b %d, %Y}")
    return LivenessResult("open")


def stated_expiry(url: str, html: str) -> datetime | None:
    """
    When the posting's own system says it expired, if that has passed.

    Dayforce keeps serving an expired posting's page, whole and with a 200 —
    the only sign is `postingExpiryTimestampUTC` in its data: the two postings
    SimplifyJobs linked had expired in March and April and read as open in
    September (measured 2026-09-28). Only Dayforce, whose system enforces the
    date, and never an evergreen posting. A JSON-LD `validThrough` is not
    trusted the same way: employers fill it in by rote and keep taking
    applications past it.
    """
    if not _host_matches(urlparse(url).hostname or "", ("dayforcehcm.com",)):
        return None
    from app.services.enrichment import dayforce_posting

    data = dayforce_posting(html)
    if not data or data.get("isEvergreen"):
        return None
    raw = data.get("postingExpiryTimestampUTC")
    if not raw:
        return None
    try:
        expiry = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry if expiry < datetime.now(timezone.utc) else None


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
    not yet known-closed, and not checked recently.

    In the order their verdict is worth most, which only matters when there
    are more of them than one sweep checks:

    1. Never checked. It is the one with no verdict at all.
    2. Not yet applied to. Whether a posting you already applied to has closed
       changes nothing you would do next.
    3. Higher score first — the job you are likeliest to apply to.
    4. Then the oldest verdict, then the newest posting.

    It used to be the oldest verdict alone, so past the budget a 95 waited
    behind every 71 that happened to be checked earlier.
    """
    from sqlalchemy import case, exists, func

    from app.models.application import Application, ApplicationStatus

    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, recheck_days))
    applied = exists().where(
        Application.job_id == Job.id,
        Application.status != ApplicationStatus.not_applied,
    )
    score = func.coalesce(Job.llm_score_deep, Job.llm_score)
    return (
        db.query(Job)
        .filter(
            Job.status.in_([JobStatus.matched, JobStatus.docs_generated]),
            Job.closed_at.is_(None),
            (Job.liveness_checked_at.is_(None)) | (Job.liveness_checked_at < cutoff),
        )
        .order_by(
            Job.liveness_checked_at.is_not(None),
            case((applied, 1), else_=0),
            score.desc().nulls_last(),
            Job.liveness_checked_at.asc().nullsfirst(),
            Job.fetched_at.desc(),
        )
        .limit(max(1, limit))
        .all()
    )


# Checks written back and committed together. A sweep that runs into its time
# limit keeps every verdict up to the last batch, rather than losing the lot —
# which is what writing back once at the end did, and a larger budget made
# likelier.
_BATCH = 50


def sweep(db, limit: int | None = None, workers: int | None = None) -> dict:
    """
    Check one budget's worth of jobs and record what was learned.

    Returns {"checked": n, "closed": n, "still_open": n, "unknown": n} plus
    "coverage" — see `coverage` for what that answers and why it is not one of
    the counters.
    """
    from app.services.tunables import live

    cfg = live()
    limit = limit if limit is not None else cfg.LIVENESS_MAX_PER_CYCLE
    workers = workers if workers is not None else cfg.LIVENESS_WORKERS

    jobs = candidates(db, limit, cfg.LIVENESS_RECHECK_DAYS)
    counts = {"checked": 0, "closed": 0, "still_open": 0, "unknown": 0}
    if not jobs:
        # Reported on the empty run too, so the key is always there for a
        # caller to read rather than present only on the runs that did work.
        counts["coverage"] = coverage(db, cfg)
        return counts

    # The network happens outside the ORM: check a batch, then write its
    # outcomes back and commit, so no transaction spans a slow site.
    from app.services.url_safety import public_client

    with public_client(
        headers=_HEADERS, timeout=REQUEST_TIMEOUT,
        follow_redirects=True, max_redirects=10,
    ) as client:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(jobs)))) as pool:
            for start in range(0, len(jobs), _BATCH):
                batch = jobs[start:start + _BATCH]
                targets = [(job, _check_target(job)) for job in batch]
                results = list(pool.map(
                    lambda t: check_url(t[1], client) if t[1]
                    else LivenessResult("unknown", "no URL stored"),
                    targets,
                ))
                now = datetime.now(timezone.utc)
                for (job, _), result in zip(targets, results):
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
    counts["coverage"] = coverage(db, cfg)
    if counts["coverage"]["sustainable"] is False:
        # Said out loud, because the failure is silent otherwise. Past the
        # budget, the lowest-priority jobs in `candidates` simply stop being
        # re-checked and keep showing a months-old "still open" — which is the
        # state this module exists to remove, moved from "never checked" to
        # "checked once".
        logger.warning(
            "liveness: %d jobs worth checking against a budget of %d a day "
            "(%d per sweep, every %dh) on a %d-day recheck — the lowest-scored "
            "verdicts will go stale. Raise \"Postings checked per sweep\" or "
            "shorten \"Check every (hours)\" on the settings page.",
            counts["coverage"]["worth_checking"], counts["coverage"]["daily_budget"],
            cfg.LIVENESS_MAX_PER_CYCLE, cfg.LIVENESS_INTERVAL_HOURS,
            cfg.LIVENESS_RECHECK_DAYS,
        )
    return counts


def coverage(db, cfg=None) -> dict:
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

    if cfg is None:
        from app.services.tunables import live

        cfg = live()
    per_day = (
        cfg.LIVENESS_MAX_PER_CYCLE
        * max(1.0, 24.0 / max(1, cfg.LIVENESS_INTERVAL_HOURS))
    )
    sustainable_population = per_day * max(1, cfg.LIVENESS_RECHECK_DAYS)
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
