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


def backlog(db: Session) -> dict:
    """
    How much is left to do, so the panel can say whether it is draining.

    Counted rather than estimated: "12,400 thin descriptions, 3,100 of them
    jobs we rejected for having none" is the sentence that makes a run of 200
    legible as progress instead of as a number with no denominator.
    """
    from sqlalchemy import or_

    from app.models.job import Job, JobStatus
    from app.services.enrichment import (
        RESCUABLE_FILTER_REASONS,
        THIN_DESCRIPTION_CHARS,
    )

    thin = or_(
        Job.description.is_(None),
        func.length(Job.description) < THIN_DESCRIPTION_CHARS,
    )
    try:
        return {
            "thin": db.query(func.count(Job.id)).filter(
                thin, Job.closed_at.is_(None)
            ).scalar() or 0,
            "rescuable": db.query(func.count(Job.id)).filter(
                Job.status == JobStatus.filtered_out,
                Job.filter_reason.in_(RESCUABLE_FILTER_REASONS),
            ).scalar() or 0,
        }
    except Exception as exc:
        logger.warning("enrichment_history: backlog unavailable: %s", exc)
        return {"thin": 0, "rescuable": 0}


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
