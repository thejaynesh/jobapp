"""
Move long-dead jobs out of the hot table, without forgetting them.

`jobs` is mostly descriptions, and most of those belong to postings the
pipeline rejected months ago. Nothing reads them again — a job filtered on a
title mismatch in June is not going to be reconsidered — and the text costs a
paragraph apiece across a hundred and fifty thousand rows.

What is not disposable is the fact that we have seen it. That is the whole
difficulty here, and it is why this moves rather than deletes.

**Deduplication is the constraint everything else bends around.** It has three
layers: the URL in `source_urls`, the source's own id, and the content hash.
Deleting a job defeats all three at once, and the failure is silent and
expensive — the next fetch re-inserts the same posting as new, spends a scoring
call, reaches the same verdict, and does it again the week after. So the
archive keeps exactly those columns and `deduplication.find_existing_job` reads
it.

**Three kinds of job are never archived**, each for a different reason:

* Anything with an application. That is the user's pipeline, and the row is
  attached to documents on disk.
* Anything the user rejected by hand (`manual`, `blocked_title`,
  `excluded_company`). Those are the labels the match-quality harness builds
  its fixture from, and it needs the description — archiving them would
  quietly destroy the only ground truth this system has about its own scoring.
* Anything still `new`, `matched` or `docs_generated`. Only settled rejections
  are old news.
"""

import logging
from datetime import datetime, timedelta, timezone

from app.config import settings

logger = logging.getLogger(__name__)

# Verdicts the user reached themselves. `services.match_eval` reads exactly
# these — with their descriptions — to build the fixture that measures whether
# a prompt change improved anything. They are the only labelled data here.
PROTECTED_REASONS = frozenset({"manual", "blocked_title", "excluded_company"})


def _days() -> int:
    try:
        return max(1, int(getattr(settings, "ARCHIVE_AFTER_DAYS", 60)))
    except (TypeError, ValueError):
        return 60


def _batch() -> int:
    try:
        return max(1, int(getattr(settings, "ARCHIVE_MAX_PER_RUN", 5000)))
    except (TypeError, ValueError):
        return 5000


def enabled() -> bool:
    return bool(getattr(settings, "ARCHIVE_ENABLED", True))


def _eligible(db, days: int | None):
    """
    The query behind both `candidates` and `remaining`.

    Shared rather than written twice: the two answer the same question — "what
    may be archived" — and a protection added to one but not the other would
    make the count on the page disagree with what the run actually does, which
    is the kind of drift nobody notices until rows are already gone.
    """
    from app.models.application import Application
    from app.models.job import Job, JobStatus

    cutoff = datetime.now(timezone.utc) - timedelta(days=_days() if days is None else days)
    return (
        db.query(Job)
        .outerjoin(Application, Application.job_id == Job.id)
        .filter(
            Job.status == JobStatus.filtered_out,
            Job.fetched_at < cutoff,
            # Never a job the user acted on: the row is attached to documents
            # on disk and to their own pipeline.
            Application.id.is_(None),
            # Never a verdict the user made — see PROTECTED_REASONS.
            (Job.filter_reason.is_(None))
            | (Job.filter_reason.notin_(tuple(PROTECTED_REASONS))),
            # Never one they starred. A favourite that was also filtered out is
            # the most explicit disagreement with the matcher there is, and it
            # is exactly the row a 60-day sweep would otherwise take.
            Job.favourite.is_(False),
        )
    )


def candidates(db, days: int | None = None, limit: int | None = None) -> list:
    """
    The jobs it is safe to archive, oldest first.

    Oldest first so a bounded run always makes progress on the worst of the
    backlog rather than skimming whatever the planner happened to return.
    """
    from app.models.job import Job

    return (
        _eligible(db, days)
        .order_by(Job.fetched_at.asc())
        .limit(_batch() if limit is None else max(1, limit))
        .all()
    )


def archive(db, days: int | None = None, limit: int | None = None) -> dict:
    """
    Move settled rejections into `archived_jobs`. Returns what it did.

    The insert happens before the delete and both are in one transaction, so
    the worst case is a rollback that leaves everything where it was. A job
    that vanished from `jobs` without a tombstone would be re-fetched, re-
    scored and re-rejected on the next cycle — which is the exact cost this is
    supposed to remove.
    """
    from app.models.archived_job import ArchivedJob
    from app.models.job import Job

    if not enabled():
        return {"archived": 0, "skipped": 0, "enabled": False}

    rows = candidates(db, days=days, limit=limit)
    if not rows:
        return {"archived": 0, "skipped": 0, "enabled": True, "remaining": 0}

    # A hash already in the archive means this posting was archived under a
    # different job row — a cross-post the dedupe layers missed at fetch time.
    # The tombstone is already doing its job, so the live row can simply go.
    seen = {
        value for (value,) in db.query(ArchivedJob.dedupe_hash).filter(
            ArchivedJob.dedupe_hash.in_([job.dedupe_hash for job in rows])
        )
    }

    archived = 0
    duplicates = 0
    doomed = []
    for job in rows:
        doomed.append(job.id)
        if job.dedupe_hash in seen:
            duplicates += 1
            continue
        seen.add(job.dedupe_hash)
        db.add(ArchivedJob(
            id=job.id,
            source=job.source,
            source_job_id=job.source_job_id,
            source_urls=list(job.source_urls or []),
            url=job.url,
            dedupe_hash=job.dedupe_hash,
            title=job.title,
            company=job.company,
            location=job.location,
            filter_reason=job.filter_reason,
            fetched_at=job.fetched_at,
            posted_at=job.posted_at,
        ))
        archived += 1

    db.flush()
    db.query(Job).filter(Job.id.in_(doomed)).delete(synchronize_session=False)
    db.commit()

    logger.info(
        "archive: moved %d job(s) out of the hot table (%d were already "
        "tombstoned under another row), %d still eligible",
        archived, duplicates, remaining(db, days=days),
    )
    return {
        "archived": archived,
        "skipped": duplicates,
        "enabled": True,
        "remaining": remaining(db, days=days),
    }


def remaining(db, days: int | None = None) -> int:
    """How many are still eligible — the caller's cue to run again."""
    return _eligible(db, days).count()


def status(db) -> dict:
    """What the funnel and the runs page show."""
    from sqlalchemy import func

    from app.models.archived_job import ArchivedJob

    total = db.query(func.count(ArchivedJob.id)).scalar() or 0
    newest = (
        db.query(func.max(ArchivedJob.archived_at)).scalar() if total else None
    )
    return {
        "enabled": enabled(),
        "total": int(total),
        "last_archived_at": newest,
        "after_days": _days(),
        "eligible": remaining(db),
    }


def reasons(db) -> dict[str, int]:
    """
    Archived counts by filter reason.

    The funnel's "why were jobs dropped" panel would otherwise lose a hundred
    thousand rows the first time this runs, and a breakdown that silently
    stops counting most of its subject is worse than one that is merely
    incomplete.
    """
    from sqlalchemy import func

    from app.models.archived_job import ArchivedJob

    rows = (
        db.query(ArchivedJob.filter_reason, func.count(ArchivedJob.id))
        .group_by(ArchivedJob.filter_reason)
        .all()
    )
    return {reason or "unknown": int(count) for reason, count in rows}
