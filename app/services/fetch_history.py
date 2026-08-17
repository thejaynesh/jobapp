"""
Recording and reading fetch-cycle history.

The profile used to hold only the latest run, so every interesting question was
unanswerable: has LinkedIn been dead for a week or did it break today? Does that
source ever contribute anything new, or does it re-fetch the same jobs forever?
Is the cycle getting slower? One data point can't answer any of those.

Each cycle now writes a `fetch_runs` row plus one `fetch_source_runs` row per
source, with what the source returned *and* what came of it — fetched versus
inserted is the difference between a source that looks busy and one that's
actually useful.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import Integer, func
from sqlalchemy.orm import Session

from app.models.fetch_run import FetchRun, FetchSourceRun

logger = logging.getLogger(__name__)

# Runs are small; keeping a few hundred costs nothing and covers weeks of
# history at the default cadence.
DEFAULT_RETENTION = 200


def record_run(
    db: Session,
    started_at: datetime,
    counts: dict,
    source_stats: dict,
    per_source_outcome: dict | None = None,
    queries: list[str] | None = None,
    locations: list[str] | None = None,
    resolve_stats: dict | None = None,
    board_stats: dict | None = None,
    backfill: dict | None = None,
    error: str | None = None,
    retention: int = DEFAULT_RETENTION,
) -> FetchRun:
    """
    Persist one cycle. `per_source_outcome` maps source → what happened to its
    jobs downstream ({"inserted": n, "merged": n, "skipped": n, "stale": n}).
    """
    from app.services.source_diagnostics import classify

    finished_at = datetime.now(timezone.utc)
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)

    resolve_stats = resolve_stats or {}
    board_stats = board_stats or {}
    per_source_outcome = per_source_outcome or {}

    failed = [
        src for src, s in (source_stats or {}).items()
        if s.get("enabled", True) and not s.get("count") and s.get("errors")
    ]
    if error:
        status = "failed"
    elif failed:
        status = "partial"
    else:
        status = "ok"

    run = FetchRun(
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=round((finished_at - started_at).total_seconds(), 1),
        status=status,
        fetched=counts.get("fetched", 0),
        inserted=counts.get("inserted", 0),
        merged=counts.get("merged", 0),
        skipped=counts.get("skipped", 0),
        stale=counts.get("stale", 0),
        queries=list(queries or []),
        locations=list(locations or []),
        links_attempted=resolve_stats.get("attempted", 0),
        links_resolved=resolve_stats.get("resolved", 0),
        links_failed=resolve_stats.get("failed", 0),
        boards_polled=_boards_polled(board_stats),
        boards_discovered=board_stats.get("discovered", 0) or 0,
        boards_sniffed=board_stats.get("sniffed", 0) or 0,
        backfill=backfill,
        error=error,
    )
    db.add(run)
    db.flush()

    # Union of both sides: a source that contributed jobs still gets a row even
    # if it never reported stats, so processed jobs are never left unattributed.
    all_sources = set(source_stats or {}) | set(per_source_outcome)
    for source in sorted(all_sources):
        outcome = per_source_outcome.get(source, {})
        stats = (source_stats or {}).get(source)
        if stats is None:
            # No stats reported, but we demonstrably processed its jobs — infer
            # the fetch count from them rather than recording a false zero.
            stats = {
                "count": sum(outcome.values()), "errors": [], "enabled": True,
            }
        db.add(FetchSourceRun(
            run_id=run.id,
            source=source,
            enabled=bool(stats.get("enabled", True)),
            status=classify(stats),
            fetched=stats.get("count", 0),
            inserted=outcome.get("inserted", 0),
            merged=outcome.get("merged", 0),
            skipped=outcome.get("skipped", 0),
            stale=outcome.get("stale", 0),
            errors=list(stats.get("errors") or [])[:5],
        ))

    db.flush()
    prune(db, retention)
    return run


def _boards_polled(board_stats: dict) -> int:
    registry = board_stats.get("registry") or {}
    return sum((info or {}).get("active", 0) for info in registry.values())


def prune(db: Session, retention: int = DEFAULT_RETENTION) -> int:
    """Drop runs beyond the retention window (source rows cascade)."""
    if retention <= 0:
        return 0
    total = db.query(func.count(FetchRun.id)).scalar() or 0
    if total <= retention:
        return 0

    cutoff = (
        db.query(FetchRun.started_at)
        .order_by(FetchRun.started_at.desc())
        .offset(retention)
        .limit(1)
        .scalar()
    )
    if cutoff is None:
        return 0
    deleted = (
        db.query(FetchRun)
        .filter(FetchRun.started_at <= cutoff)
        .delete(synchronize_session=False)
    )
    if deleted:
        logger.info("fetch_history: pruned %d old runs", deleted)
    return deleted


def recent_runs(db: Session, limit: int = 20) -> list[FetchRun]:
    """Most recent cycles, newest first."""
    return (
        db.query(FetchRun)
        .order_by(FetchRun.started_at.desc())
        .limit(limit)
        .all()
    )


def failing_streaks(db: Session, lookback: int = 40) -> dict[str, int]:
    """
    How many most-recent runs each source has failed in a row.

    Runs where the source was disabled or skipped do not count and do not
    interrupt the streak — otherwise resting a source would immediately reset
    its own streak and it would be retried every cycle, which is the thing
    resting exists to stop.
    """
    recent = recent_runs(db, lookback)
    if not recent:
        return {}

    rows = (
        db.query(FetchSourceRun.run_id, FetchSourceRun.source, FetchSourceRun.status)
        .filter(FetchSourceRun.run_id.in_([r.id for r in recent]))
        .all()
    )
    by_run: dict = {}
    for run_id, source, status in rows:
        by_run.setdefault(run_id, {})[source] = status

    streaks: dict[str, int] = {}
    done: set[str] = set()
    for run in recent:  # newest first
        for source, status in (by_run.get(run.id) or {}).items():
            if source in done or status in ("disabled", "skipped"):
                continue
            if status == "failed":
                streaks[source] = streaks.get(source, 0) + 1
            else:
                done.add(source)
    return streaks


def resting_sources(
    db: Session, threshold: int = 10, retry_every: int = 10,
) -> dict[str, int]:
    """
    Sources to skip this cycle because they have been failing for a long time.

    An expired API key answers identically forever — JSearch has 403'd on every
    run for twenty runs — and calling it each cycle buys nothing but an error
    line that trains everyone to ignore error lines. Resting is not removal
    though: every `retry_every` runs one probe goes out, so a key the user
    refreshes is picked up on its own without anybody remembering to re-enable
    anything.

    Returns {source: streak length} for the sources being rested right now.
    """
    if threshold <= 0:
        return {}
    streaks = {
        source: streak
        for source, streak in failing_streaks(db).items()
        if streak >= threshold
    }
    if not streaks:
        return {}

    total_runs = db.query(func.count(FetchRun.id)).scalar() or 0
    if retry_every > 0 and total_runs % retry_every == 0:
        logger.info("fetch_history: re-probing rested sources %s", sorted(streaks))
        return {}
    return streaks


def source_totals(db: Session, runs: int = 20) -> list[dict]:
    """
    Per-source rollup over the last `runs` cycles, worst contributors last.

    This is the view that answers "is this source worth keeping?" — a source
    fetching thousands of jobs while inserting none is doing nothing but
    burning requests, and that only shows up across runs.
    """
    recent = [r.id for r in recent_runs(db, runs)]
    if not recent:
        return []

    rows = (
        db.query(
            FetchSourceRun.source,
            func.count(FetchSourceRun.id),
            func.sum(FetchSourceRun.fetched),
            func.sum(FetchSourceRun.inserted),
            func.sum(FetchSourceRun.merged),
            func.sum(func.cast(FetchSourceRun.status == "failed", Integer)),
        )
        .filter(FetchSourceRun.run_id.in_(recent))
        .group_by(FetchSourceRun.source)
        .all()
    )

    totals = [
        {
            "source": source,
            "runs": int(run_count or 0),
            "fetched": int(fetched or 0),
            "inserted": int(inserted or 0),
            "merged": int(merged or 0),
            "failed_runs": int(failed or 0),
        }
        for source, run_count, fetched, inserted, merged, failed in rows
    ]
    totals.sort(key=lambda t: (-t["inserted"], -t["fetched"], t["source"]))
    return totals
