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

        missed = (
            db.query(Application)
            .join(Job, Application.job_id == Job.id)
            .filter(
                Job.status.in_([JobStatus.matched, JobStatus.docs_generated]),
                Application.generation_status.in_(NEEDS_GENERATION),
            )
            .all()
        )
        for app in missed:
            # A 'failed' one has an error the user can read and a Rewrite
            # button; re-queueing it on a timer would just burn LLM calls on
            # the same failure. Only never-started ones are swept.
            if app.generation_status != "idle":
                continue
            if any(doc.is_current for doc in app.documents):
                continue
            if queue_generation(app.id):
                # Marked in-flight with a fresh clock for the same reason as the
                # stale loop: an 'idle' row would be re-queued on every sweep
                # until a worker picks the first copy up. If the queued task is
                # lost, the stale sweep recovers it after the window.
                app.generation_status = "generating"
                app.generation_started_at = now
                requeued_missed += 1

        if requeued_stale or requeued_missed:
            db.commit()
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
