"""
Rewrite the documents that were written from a thinner posting.

Enrichment goes back for the description the source left out, and it is
routinely the difference between 500 characters of teaser and the real
posting. Anything generated before that arrived was grounded in the teaser:
the resume was tailored to requirements nobody had read yet, and the cover
letter argued for a job as described by an aggregator's summary.

The badge for this already existed — "the job description became fuller after
these documents were generated" beside the Rewrite button — and it put the
work on the user: notice the notice, click the button, once per application.
This does it on a clock instead.

**What it refuses to touch, and why.** Automatic rewriting is only safe
because the set it applies to is narrow:

* Anything past `not_applied`. Once a document has been sent, the copy on
  disk is a record of what the employer received, and replacing it destroys
  the only evidence of what was actually claimed.
* Anything mid-generation or failed. A failed one has an error the user can
  read and a button to retry; re-queueing it on a timer burns calls on the
  same failure.
* Anything with no current documents at all. That is `sweep_generations`'
  job, and two sweepers queueing the same application is how you get two
  workers writing to the same rows.

**What it carries forward.** The user's own feedback. A rewrite the user
asked for ("lead with the Kafka work") is stored on the document that
answered it, and regenerating without it would silently undo an instruction
they gave — the refresh would read as the system overruling them. The most
recent feedback goes into the new run.
"""

import logging

from app.config import settings

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return bool(getattr(settings, "DOC_REFRESH_ENABLED", True))


def _limit() -> int:
    try:
        return max(0, int(getattr(settings, "DOC_REFRESH_MAX_PER_RUN", 25)))
    except (TypeError, ValueError):
        return 25


def current_documents(application) -> list:
    return [doc for doc in (application.documents or []) if doc.is_current]


def carried_feedback(application) -> str | None:
    """
    The user's most recent rewrite instruction, so the refresh keeps it.

    Without this, a generation the user steered by hand is quietly reverted to
    the unsteered version the next time the description grows.
    """
    docs = [doc for doc in current_documents(application)
            if doc.generation_feedback and doc.created_at is not None]
    if not docs:
        return None
    return max(docs, key=lambda doc: doc.created_at).generation_feedback


def stale_applications(db, limit: int | None = None) -> list:
    """
    Applications whose documents were written before the posting filled out.

    "The newest current document is older than the description stamp" is an
    aggregate over a join, so it is done in SQL — the alternative loads every
    unsent application and its documents to throw almost all of them away.

    The result is then re-checked with `Application.documents_are_stale`, which
    is the same predicate the badge on the application page renders from. The
    SQL is a bounded pre-filter and that property is the definition; if the two
    ever drift, nothing gets rewritten that the page didn't call stale.
    """
    from sqlalchemy import func, select
    from sqlalchemy.orm import selectinload

    from app.models.application import (
        Application, ApplicationDocument, ApplicationStatus,
    )
    from app.models.job import Job

    newest = (
        select(
            ApplicationDocument.application_id.label("application_id"),
            func.max(ApplicationDocument.created_at).label("written_at"),
        )
        .where(ApplicationDocument.is_current.is_(True))
        .group_by(ApplicationDocument.application_id)
        .subquery()
    )

    query = (
        db.query(Application)
        .join(Job, Application.job_id == Job.id)
        # An inner join, so applications with no current documents at all are
        # absent rather than eligible — those belong to `sweep_generations`,
        # and two sweepers queueing one application is two workers writing to
        # the same rows.
        .join(newest, newest.c.application_id == Application.id)
        .options(selectinload(Application.documents))
        .filter(
            Job.description_updated_at.isnot(None),
            newest.c.written_at < Job.description_updated_at,
            # Sent is sent: the document on disk is the record of what the
            # employer received.
            Application.status == ApplicationStatus.not_applied,
            # 'generating' is a run in flight; 'failed' has an error to read
            # and a button to retry. Neither wants a second task.
            Application.generation_status == "idle",
        )
        # Freshest evidence first: the job whose description just arrived is
        # the one whose documents are most wrong.
        .order_by(Job.description_updated_at.desc())
    )
    if limit is not None:
        query = query.limit(limit)
    return [app for app in query.all() if app.documents_are_stale]


def refresh_stale_documents(db, limit: int | None = None) -> dict:
    """
    Queue a rewrite for each application whose documents went stale.

    Bounded per run on purpose. The first pass after enrichment has been
    running for a while can find hundreds, and queueing all of them at once
    means the documents for a job the user is looking at right now sit behind
    a backlog of refreshes for jobs they are not.
    """
    from datetime import datetime, timezone

    from app.tasks.generate import queue_generation

    if not _enabled():
        return {"eligible": 0, "queued": 0, "skipped": 0, "enabled": False}

    limit = _limit() if limit is None else limit
    if limit <= 0:
        return {"eligible": 0, "queued": 0, "skipped": 0, "enabled": True}

    candidates = stale_applications(db, limit=limit)
    queued = 0
    now = datetime.now(timezone.utc)
    for application in candidates:
        feedback = carried_feedback(application)
        if not queue_generation(application.id, feedback=feedback):
            continue
        # Marked in flight with a clock, for the same reason `sweep_generations`
        # does it: an 'idle' row is re-selected by the next pass until a worker
        # picks the first copy up, and one stale application becomes a pile of
        # duplicate tasks. If the queued task is lost, the stale sweep recovers
        # it after the window.
        application.generation_status = "generating"
        application.generation_started_at = now
        queued += 1

    if queued:
        db.commit()
        logger.info(
            "refresh_stale_documents — queued %d rewrite(s) for documents that "
            "predate a fuller description", queued,
        )
    return {
        "eligible": len(candidates),
        "queued": queued,
        "skipped": len(candidates) - queued,
        "enabled": True,
    }
