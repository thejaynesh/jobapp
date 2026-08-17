"""
The pass that goes back for the descriptions the sources left out.

Runs on its own schedule *and* as a tail-call from every fetch cycle. The
schedule drains the backlog of jobs stored before enrichment existed; the
tail-call is what matters day to day, because a job enriched within minutes of
arriving is scored by the matcher on its real description rather than on a
500-character stub — and a job scored on a stub is a job the user never sees.
"""

import argparse
import logging

from app.celery_app import celery_app
from app.config import settings
from app.database import SessionLocal
from app.services.fetch_lock import acquire, release

logger = logging.getLogger(__name__)

# Its own lock key. Enrichment and fetching don't conflict, but two enrichment
# passes at once would work the same targets and double every request.
ENRICH_LOCK_KEY = "jobapp:enrich:running"
ENRICH_LOCK_TTL_SECONDS = 1800

_EMPTY = {
    "attempted": 0, "enriched": 0, "unchanged": 0, "failed": 0,
    "chars_gained": 0, "requeued_for_matching": 0, "queued_browser": 0,
}


# Under 29 minutes, like matching: with late acks the broker redelivers
# anything unacknowledged after its visibility timeout, so a task allowed to
# run longer would be handed to a second worker while the first still has it.
@celery_app.task(
    name="app.tasks.enrich.enrich_jobs",
    bind=False,
    soft_time_limit=1500,
    time_limit=1740,
)
def enrich_jobs(limit: int | None = None, match_after: bool = True) -> dict:
    """
    Enrich one batch, then hand the rescued jobs back to matching.

    Bounded per run rather than exhaustive: the backlog is tens of thousands of
    jobs, and a pass with no ceiling holds a worker slot for hours while the
    jobs it already rescued wait behind it to be scored.
    """
    if not settings.ENRICH_ENABLED:
        return {**_EMPTY, "skipped_reason": "disabled"}

    if not acquire(ttl=ENRICH_LOCK_TTL_SECONDS, key=ENRICH_LOCK_KEY):
        logger.info("enrich_jobs: another pass is already running; skipping")
        return {**_EMPTY, "skipped_reason": "already running"}

    db = SessionLocal()
    try:
        from app.services.enrichment import run

        result = run(db, limit=limit)
        # Only when something was actually rescued. A pass that improved
        # nothing has nothing new for the matcher to score, and queueing it
        # anyway would spin a matching pass over the same backlog.
        if match_after and result.get("requeued_for_matching"):
            from app.tasks.match import match_jobs
            match_jobs.delay()
        return result
    except Exception as exc:
        logger.error("enrich_jobs task raised unexpectedly: %s", exc)
        db.rollback()
        return dict(_EMPTY)
    finally:
        db.close()
        release(key=ENRICH_LOCK_KEY)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None,
                        help="jobs to attempt in this pass")
    parser.add_argument("--passes", type=int, default=1,
                        help="run this many passes back to back")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )

    from app.services.enrichment import run

    for index in range(max(1, args.passes)):
        db = SessionLocal()
        try:
            counts = run(db, limit=args.limit)
        finally:
            db.close()
        print(f"\nEnrichment pass {index + 1}")
        print("-------------------")
        for key, value in counts.items():
            print(f"  {key:22} {value}")
        if not counts.get("attempted"):
            print("\n(nothing left to enrich)")
            break


if __name__ == "__main__":
    main()
