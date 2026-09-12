"""
Recording and reading enrichment history.

Same shape as `fetch_history`, and for the same reason: one run's numbers say
almost nothing, while a column of them says whether the ATS shortcuts still
work, whether a host has started refusing us, and whether the backlog is
actually draining. The one number worth watching is `chars_gained` — it is the
whole point of the feature, stated in the only unit that cannot be faked by
doing more work.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.enrichment_run import EnrichmentRun

logger = logging.getLogger(__name__)

DEFAULT_RETENTION = 200


def record_run(
    db: Session,
    started_at: datetime,
    stats,
    error: str | None = None,
    retention: int = DEFAULT_RETENTION,
) -> EnrichmentRun:
    """Persist one pass. `stats` is an `enrichment.EnrichStats`."""
    finished_at = datetime.now(timezone.utc)
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)

    via = dict(getattr(stats, "via", {}) or {})
    if error:
        status = "failed"
    elif stats.failed and not stats.enriched:
        status = "failed"
    elif stats.failed:
        status = "partial"
    else:
        status = "ok"

    run = EnrichmentRun(
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=round((finished_at - started_at).total_seconds(), 1),
        status=status,
        attempted=stats.attempted,
        enriched=stats.enriched,
        unchanged=stats.unchanged,
        failed=stats.failed,
        via_ats_api=via.get("ats_api", 0),
        via_json_ld=via.get("json_ld", 0),
        via_llm=via.get("llm", 0),
        via_landing_html=via.get("landing_html", 0),
        queued_browser=stats.queued_browser,
        chars_gained=stats.chars_gained,
        requeued_for_matching=stats.requeued_for_matching,
        # Only the worst offenders: a run that fails on 200 distinct hosts
        # would otherwise store 200 keys nobody reads.
        failures_by_host=dict(
            sorted(
                (stats.failures_by_host or {}).items(),
                key=lambda kv: -kv[1],
            )[:20]
        ) or None,
        error=error,
    )
    db.add(run)
    db.flush()
    prune(db, retention)
    return run


def prune(db: Session, retention: int = DEFAULT_RETENTION) -> int:
    """Drop runs beyond the retention window."""
    if retention <= 0:
        return 0
    total = db.query(func.count(EnrichmentRun.id)).scalar() or 0
    if total <= retention:
        return 0

    cutoff = (
        db.query(EnrichmentRun.started_at)
        .order_by(EnrichmentRun.started_at.desc())
        .offset(retention)
        .limit(1)
        .scalar()
    )
    if cutoff is None:
        return 0
    deleted = (
        db.query(EnrichmentRun)
        .filter(EnrichmentRun.started_at <= cutoff)
        .delete(synchronize_session=False)
    )
    if deleted:
        logger.info("enrichment_history: pruned %d old runs", deleted)
    return deleted


def recent_runs(db: Session, limit: int = 15) -> list[EnrichmentRun]:
    """Most recent passes, newest first."""
    return (
        db.query(EnrichmentRun)
        .order_by(EnrichmentRun.started_at.desc())
        .limit(limit)
        .all()
    )


def _waiting(db: Session, thin) -> int:
    """Thin jobs enrichment can pick up now, cooloff respected."""
    from datetime import timedelta

    from sqlalchemy import or_

    from app.config import settings
    from app.models.job import Job

    retry_after = datetime.now(timezone.utc) - timedelta(
        days=max(0, int(getattr(settings, "ENRICH_RETRY_DAYS", 7)))
    )
    return db.query(func.count(Job.id)).filter(
        thin,
        Job.closed_at.is_(None),
        or_(
            Job.enrichment_attempted_at.is_(None),
            Job.enrichment_attempted_at < retry_after,
        ),
    ).scalar() or 0


# Where the panel's backlog numbers are kept between page loads, and how long
# they are allowed to be stale.
#
# Not a nicety. Counting them costs a parallel sequential scan that de-TOASTs
# every description on the table — measured at 117 seconds and five gigabytes
# of reads against 300,941 rows — because 55% of the table matches the thin
# predicate and no index is selective enough for the planner to prefer it. It
# is not a missing index; on that selectivity a seq scan is genuinely cheaper,
# which is why one exists already (`ix_jobs_enrichment_targets`) and is
# correctly ignored.
#
# Three of those ran concurrently on a live box, each repeating the others'
# work because three requests arrived while the first was still going. The
# cache is what stops a page refresh from being a denial of service.
#
# Ten minutes because this is a progress indicator on a backlog that drains
# over days. Nobody watching it can tell a ten-minute-old number from a fresh
# one, and the honest fix — storing the length so it can be indexed — is a
# table rewrite that wants its own change.
BACKLOG_KEY = "jobapp:enrichment:backlog"
BACKLOG_TTL_SECONDS = 600


def _cached_backlog() -> dict | None:
    try:
        import json

        import redis

        from app.config import settings

        raw = redis.Redis.from_url(
            settings.REDIS_URL, socket_timeout=2
        ).get(BACKLOG_KEY)
        return json.loads(raw) if raw else None
    except Exception as exc:
        # A cache that cannot be reached must not cost the panel its numbers,
        # and must not cost them slowly either — hence the short timeout.
        logger.debug("enrichment_history: backlog cache unavailable: %s", exc)
        return None


def _store_backlog(counts: dict) -> None:
    try:
        import json

        import redis

        from app.config import settings

        redis.Redis.from_url(settings.REDIS_URL, socket_timeout=2).setex(
            BACKLOG_KEY, BACKLOG_TTL_SECONDS, json.dumps(counts)
        )
    except Exception as exc:
        logger.debug("enrichment_history: could not cache the backlog: %s", exc)


def backlog(db: Session, refresh: bool = False) -> dict:
    """
    How much is left to do, so the panel can say whether it is draining.

    Counted rather than estimated: "12,400 thin descriptions, 3,100 of them
    jobs we rejected for having none" is the sentence that makes a run of 200
    legible as progress instead of as a number with no denominator.

    Cached for `BACKLOG_TTL_SECONDS`, because the count is a two-minute
    sequential scan over every description on the table — see `BACKLOG_KEY`.
    Pass `refresh` to pay for it deliberately.
    """
    from sqlalchemy import or_

    from app.models.job import Job, JobStatus
    from app.services.enrichment import (
        RESCUABLE_FILTER_REASONS,
        THIN_DESCRIPTION_CHARS,
    )

    if not refresh:
        cached = _cached_backlog()
        if cached is not None:
            return cached

    thin = or_(
        Job.description.is_(None),
        func.length(Job.description) < THIN_DESCRIPTION_CHARS,
    )
    try:
        counts = {
            "thin": db.query(func.count(Job.id)).filter(
                thin, Job.closed_at.is_(None)
            ).scalar() or 0,
            # What is actually reachable right now. `thin` counts every job
            # with a poor description; most of the difference is jobs already
            # tried and cooling off, and a panel that only showed the larger
            # number would read as a backlog that refuses to drain.
            "waiting": _waiting(db, thin),
            "rescuable": db.query(func.count(Job.id)).filter(
                Job.status == JobStatus.filtered_out,
                Job.filter_reason.in_(RESCUABLE_FILTER_REASONS),
            ).scalar() or 0,
        }
    except Exception as exc:
        logger.warning("enrichment_history: backlog unavailable: %s", exc)
        # Deliberately not cached. Zeros are the failure, not the answer, and
        # storing them would show an empty backlog for ten minutes after one
        # bad query.
        return {"thin": 0, "waiting": 0, "rescuable": 0}

    _store_backlog(counts)
    return counts


def linkedin_state(db: Session) -> dict:
    """
    How LinkedIn is doing, and whether the harvest is switched on.

    The harvest is built and, so far, produces zero — the extension toggle
    that feeds it has never been ticked. A subsystem that works perfectly and
    is never enabled looks identical, from the server, to one that is broken;
    saying "0 harvested, 8,800 without a description" makes the difference
    visible on the page where somebody might act on it.
    """
    from app.models.job import Job

    from app.services.harvest import HARVEST_SOURCES

    try:
        harvested = db.query(func.count(Job.id)).filter(
            Job.source.in_(sorted(set(HARVEST_SOURCES.values())))
        ).scalar() or 0
        missing = db.query(func.count(Job.id)).filter(
            Job.source == "linkedin",
            func.coalesce(func.length(Job.description), 0) == 0,
        ).scalar() or 0
        return {"harvested": int(harvested), "without_description": int(missing)}
    except Exception as exc:
        logger.warning("enrichment_history: LinkedIn state unavailable: %s", exc)
        return {}


def totals(db: Session, runs: int = 20) -> dict:
    """Rollup across the last `runs` passes."""
    recent = recent_runs(db, runs)
    if not recent:
        return {}
    return {
        "runs": len(recent),
        "attempted": sum(r.attempted for r in recent),
        "enriched": sum(r.enriched for r in recent),
        "chars_gained": sum(r.chars_gained for r in recent),
        "requeued_for_matching": sum(r.requeued_for_matching for r in recent),
        "queued_browser": sum(r.queued_browser for r in recent),
        "via": {
            "ats_api": sum(r.via_ats_api for r in recent),
            "json_ld": sum(r.via_json_ld for r in recent),
            "llm": sum(r.via_llm for r in recent),
            "landing_html": sum(r.via_landing_html for r in recent),
        },
    }
