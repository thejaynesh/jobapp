import logging
from typing import Any

from app.celery_app import celery_app
from app.config import settings
from app.database import SessionLocal
from app.services.fetch_lock import acquire, release
from app.services.matcher import match_all_new_jobs

logger = logging.getLogger(__name__)

# Its own key. Matching and fetching don't conflict, so sharing the fetch lock
# would have each block the other for no reason.
MATCH_LOCK_KEY = "jobapp:match:running"
# Long enough to cover a batch of slow reasoning-model calls, short enough that
# a worker killed mid-batch doesn't wedge matching until someone notices.
MATCH_LOCK_TTL_SECONDS = 1800

_EMPTY = {"processed": 0, "matched": 0, "filtered_out": 0, "errors": 0}


# Under 29 minutes on purpose. With late acks the Redis broker redelivers
# anything still unacknowledged after its visibility timeout — an hour by
# default — so a task allowed to run longer than that would be handed to a
# second worker while the first is still working on it.
@celery_app.task(
    name="app.tasks.match.match_jobs",
    bind=False,
    soft_time_limit=1500,
    time_limit=1740,
)
def match_jobs(limit: int | None = None) -> dict[str, Any]:
    """
    Score one batch of new jobs, then hand off.

    Bounded rather than exhaustive, for three reasons. A pass over a large
    backlog is minutes of LLM round trips holding one of only two worker slots,
    so the generations it queues cannot start until it is completely finished.
    A worker restarted anywhere in that window used to lose the whole pass. And
    a single task with no time limit that stops making progress looks identical
    to one that is merely slow.

    Batches re-queue themselves while there is more to do and something
    actually moved. "Nothing moved" means the provider is refusing calls — every
    job comes back rate-limited and stays `new` — and chaining there would spin
    against a wall, so it stops and lets the schedule retry in a few minutes.
    """
    batch = limit or max(1, settings.MATCH_MAX_JOBS_PER_TASK)

    # Two beat ticks, a fetch tail-call and a manual trigger can all land at
    # once; overlapping passes would score the same jobs twice and double the
    # LLM spend for it.
    if not acquire(ttl=MATCH_LOCK_TTL_SECONDS, key=MATCH_LOCK_KEY):
        logger.info("match_jobs: another pass is already running; skipping")
        return {**_EMPTY, "skipped_reason": "already running"}

    db = SessionLocal()
    queued = 0
    result: dict | None = None
    try:
        def on_matched(job) -> None:
            """Queue documents as each job matches, not after the whole pass."""
            nonlocal queued
            from app.tasks.generate import NEEDS_GENERATION, queue_generation

            for app in job.applications:
                if app.generation_status in NEEDS_GENERATION and queue_generation(app.id):
                    queued += 1

        result = match_all_new_jobs(db, limit=batch, on_matched=on_matched)
        result["queued_for_generation"] = queued
        logger.info(
            "match_jobs batch complete — processed=%d matched=%d filtered_out=%d "
            "errors=%d queued_for_generation=%d remaining=%d",
            result["processed"], result["matched"], result["filtered_out"],
            result["errors"], queued, result["remaining"],
        )
        return result
    except Exception as exc:
        logger.error("match_jobs task raised unexpectedly: %s", exc)
        return {**_EMPTY, "errors": 1, "remaining": 0}
    finally:
        db.close()
        # Released before the follow-up is published, or the next batch would
        # find the lock still held and skip itself.
        release(key=MATCH_LOCK_KEY)
        _chain_if_more(result)


def _chain_if_more(result: dict | None) -> None:
    """
    Queue the next batch, or close the cycle out.

    Every path that stops chaining also clears the shared call budget: the
    batches spend one ceiling between them, so "the cycle" ends exactly where
    the chain does, and a cycle that started against the last one's spend would
    do no paid failover at all.
    """
    from app.services import match_budget

    if not result or not result.get("remaining"):
        match_budget.clear()
        return
    if not (result.get("matched", 0) or result.get("filtered_out", 0)):
        # Every job in the batch came back rate-limited or errored. Another
        # batch now would hit the same wall; the schedule retries soon enough.
        logger.warning(
            "match_jobs: no progress on this batch (%d still waiting) — "
            "leaving the rest to the next scheduled pass",
            result["remaining"],
        )
        match_budget.clear()
        return
    try:
        match_jobs.delay()
    except Exception as exc:
        logger.error("match_jobs: could not queue the next batch: %s", exc)
