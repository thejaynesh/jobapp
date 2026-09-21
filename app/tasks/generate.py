import errno
import logging
import uuid
from datetime import datetime, timedelta, timezone

from celery.exceptions import SoftTimeLimitExceeded

from app.celery_app import celery_app
from app.config import settings
from app.database import SessionLocal
from app.models.application import Application

logger = logging.getLogger(__name__)

# Statuses that mean "no run is in flight and none has succeeded", so queueing
# one is the right move. 'generating' is excluded on purpose — the sweeper
# decides about those, using the clock.
NEEDS_GENERATION = ("idle", "failed")


def queue_generation(application_id, feedback: str | None = None) -> bool:
    """
    Ask a worker to write this application's documents.

    Separate from `.delay()` at the call sites so that a broker that is down
    is a logged failure rather than an exception thrown through whatever was
    happening at the time — a matching pass, in particular, should not lose
    its remaining jobs because Redis blinked between two of them.

    `feedback` is the user's instruction for this rewrite. The automatic
    refresh passes the last one they gave, so a run it did not ask for cannot
    quietly undo a run it did.
    """
    try:
        generate_docs.delay(str(application_id), feedback=feedback)
        return True
    except Exception as exc:
        logger.error("could not queue generation for %s: %s", application_id, exc)
        return False


def claim_for_generation(db, application_id, allowed=NEEDS_GENERATION) -> bool:
    """
    Take this application's generation slot, or report that somebody else has.

    A conditional UPDATE, because every writer here used to select rows in one
    statement and write `generation_status` in another — `stale_applications`
    filters on `'idle'`, `sweep_generations` on `NEEDS_GENERATION`, and both
    then set `'generating'` later in the loop. Two overlapping passes could
    each see the same `'idle'` row and each queue a task for it, and two
    workers writing one application's documents is two resumes racing to the
    same rows.

    Returning the row count from the UPDATE's own `WHERE` makes the check and
    the claim one statement, so exactly one caller can win. Committed here
    rather than left to the caller: an uncommitted claim is not a claim, and
    the whole point is that a second pass reading the row sees it taken.

    Claim first, dispatch second — the caller undoes it if the broker refuses
    the task (see `release_generation_claim`). The other order would dispatch
    work nobody had reserved.
    """
    from datetime import datetime, timezone

    claimed = (
        db.query(Application)
        .filter(
            Application.id == application_id,
            Application.generation_status.in_(tuple(allowed)),
        )
        .update(
            {
                "generation_status": "generating",
                "generation_started_at": datetime.now(timezone.utc),
            },
            synchronize_session="fetch",
        )
    )
    if claimed:
        db.commit()
        return True
    db.rollback()
    return False


def release_generation_claim(db, application_id) -> None:
    """
    Hand a claimed slot back, for when the task could not be queued.

    Without this a broker blip would leave the row reading `'generating'` with
    no task behind it, and nothing would touch it again until
    `GENERATION_STUCK_MINUTES` elapsed — a twenty-minute wait to retry
    something that failed instantly. Back to `'idle'`, so the next pass picks
    it up.
    """
    db.query(Application).filter(
        Application.id == application_id,
        Application.generation_status == "generating",
    ).update(
        {"generation_status": "idle", "generation_started_at": None},
        synchronize_session="fetch",
    )
    db.commit()


def _friendly_error(exc: BaseException) -> str:
    if isinstance(exc, OSError) and exc.errno == errno.EAGAIN:
        return (
            "The server was temporarily out of resources (memory/processes). "
            "It usually recovers in a minute — please retry."
        )
    return str(exc)


@celery_app.task(
    name="app.tasks.generate.generate_docs",
    bind=True,
    soft_time_limit=300,
    time_limit=360,
    max_retries=2,
)
def generate_docs(self, application_id: str, feedback: str | None = None) -> dict:
    db = SessionLocal()
    try:
        app = db.query(Application).filter(Application.id == uuid.UUID(application_id)).first()
        if not app:
            logger.warning("generate_docs: application %s not found", application_id)
            return {"status": "not_found"}

        app.generation_status = "generating"
        app.generation_error = None
        app.generation_started_at = datetime.now(timezone.utc)
        db.commit()

        from app.services.doc_generator import generate_documents
        generate_documents(db, app, feedback=feedback)

        app = db.query(Application).filter(Application.id == uuid.UUID(application_id)).first()
        if app:
            app.generation_status = "done"
            db.commit()

        return {"status": "ok", "application_id": application_id}

    except SoftTimeLimitExceeded:
        logger.error("generate_docs timed out for %s", application_id)
        # Discard whatever the interrupted generation had pending (a document
        # row, is_current flips) — without this, _mark_failed's commit would
        # write that partial state alongside the failure.
        db.rollback()
        _mark_failed(db, application_id, "Generation timed out after 5 minutes")
        return {"status": "timeout"}

    except Exception as exc:
        db.rollback()
        # Transient resource exhaustion (fork/thread EAGAIN): retry the whole
        # task after a pause instead of failing the generation outright.
        if (
            isinstance(exc, OSError)
            and exc.errno == errno.EAGAIN
            and self.request.retries < self.max_retries
        ):
            logger.warning(
                "generate_docs EAGAIN for %s — retrying task (attempt %d)",
                application_id, self.request.retries + 1,
            )
            raise self.retry(countdown=20, exc=exc)
        logger.error("generate_docs failed for %s: %s", application_id, exc)
        _mark_failed(db, application_id, _friendly_error(exc))
        return {"status": "error", "error": str(exc)}
    finally:
        db.close()


@celery_app.task(name="app.tasks.generate.sweep_generations", bind=False)
def sweep_generations() -> dict:
    """
    Re-queue generations that nothing else is going to finish.

    Two ways an application ends up waiting forever with no error recorded:

    * its worker was killed mid-run — a deploy, an OOM — leaving the row at
      'generating' with no task behind it. Late acks (see celery_app) fix this
      going forward, but not for tasks already lost, and not when the same
      worker is killed twice.
    * it was never queued at all, because the pass that should have queued it
      did not get that far.

    Both are indistinguishable from "working on it" by looking at the app,
    which is why this runs on a clock rather than waiting to be noticed.
    """
    from app.models.job import Job, JobStatus

    cutoff = datetime.now(timezone.utc) - timedelta(
        minutes=max(1, settings.GENERATION_STUCK_MINUTES)
    )
    db = SessionLocal()
    requeued_stale = 0
    requeued_missed = 0
    try:
        stale = (
            db.query(Application)
            .filter(
                Application.generation_status == "generating",
                # A NULL start time means the row predates the column, and the
                # migration stamped those, so anything NULL here started under
                # code that no longer runs. Treat it as stale.
                (Application.generation_started_at.is_(None))
                | (Application.generation_started_at < cutoff),
            )
            .all()
        )
        now = datetime.now(timezone.utc)
        for app in stale:
            if queue_generation(app.id):
                # Restart the clock at requeue time. Without this the row keeps
                # its old start time, so while the queue is backed up every
                # sweep re-queues the same application again — one stuck run
                # becomes a pile of duplicate tasks.
                app.generation_started_at = now
                requeued_stale += 1

        # Bounded, and with the documents loaded in one query rather than one
        # per row. This read every matched application on every sweep — every
        # twenty minutes — and then touched `app.documents` per row, so a large
        # matched backlog made an unbounded scan plus an N+1 on a timer.
        from sqlalchemy.orm import selectinload

        missed = (
            db.query(Application)
            .join(Job, Application.job_id == Job.id)
            .options(selectinload(Application.documents))
            .filter(
                Job.status.in_([JobStatus.matched, JobStatus.docs_generated]),
                Application.generation_status.in_(NEEDS_GENERATION),
            )
            .limit(max(1, settings.GENERATION_SWEEP_MAX_PER_RUN))
            .all()
        )
        # Decided before anything commits, and reduced to bare ids.
        #
        # `claim_for_generation` commits, and `expire_on_commit` is on, so
        # reading `app.generation_status` or `app.documents` inside the claim
        # loop would expire and reload every row — one query per application,
        # which is the N+1 the `selectinload` above exists to remove, made
        # worse. Two passes: read, then write.
        #
        # A 'failed' one has an error the user can read and a Rewrite button;
        # re-queueing it on a timer would just burn LLM calls on the same
        # failure. Only never-started ones are swept.
        wanted = [
            app.id for app in missed
            if app.generation_status == "idle"
            and not any(doc.is_current for doc in app.documents)
        ]
        # The stale loop's re-queues go out before any claim commits, so the
        # two writes cannot interleave in one transaction.
        if requeued_stale:
            db.commit()

        for application_id in wanted:
            # Claimed before dispatch, and in one statement: marking the row
            # in flight is what stops an 'idle' row being re-queued on every
            # sweep until a worker picks the first copy up, but doing it after
            # the dispatch left a window for `refresh_stale_docs` — the other
            # writer of this column — to claim the same row. If the queued task
            # is lost, the stale loop above recovers it after the window.
            if not claim_for_generation(db, application_id, allowed=("idle",)):
                continue
            if queue_generation(application_id):
                requeued_missed += 1
            else:
                release_generation_claim(db, application_id)

        if requeued_stale or requeued_missed:
            logger.info(
                "sweep_generations — requeued %d stalled, %d never queued",
                requeued_stale, requeued_missed,
            )
        return {"stalled": requeued_stale, "never_queued": requeued_missed}
    except Exception as exc:
        logger.error("sweep_generations failed: %s", exc)
        return {"stalled": 0, "never_queued": 0, "error": str(exc)}
    finally:
        db.close()


@celery_app.task(name="app.tasks.generate.refresh_stale_docs", bind=False)
def refresh_stale_docs() -> dict:
    """
    Rewrite documents that were written from a thinner posting.

    Enrichment goes back for the description the source left out, and it is
    routinely the difference between an aggregator's teaser and the real
    posting. The badge for this already existed and put the work on the user:
    notice it, click Rewrite, once per application. See `services.doc_refresh`
    for what it refuses to touch and why.
    """
    from app.services import doc_refresh

    db = SessionLocal()
    try:
        return doc_refresh.refresh_stale_documents(db)
    except Exception as exc:
        db.rollback()
        logger.error("refresh_stale_docs failed: %s", exc)
        return {"eligible": 0, "queued": 0, "error": str(exc)}
    finally:
        db.close()


def _mark_failed(db, application_id: str, error: str) -> None:
    try:
        app = db.query(Application).filter(Application.id == uuid.UUID(application_id)).first()
        if app:
            app.generation_status = "failed"
            app.generation_error = error[:500]
            db.commit()
    except Exception as exc:
        logger.error("generate_docs: could not save failure state: %s", exc)
