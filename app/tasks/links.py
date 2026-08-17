"""
One-time repair of the apply URLs that stopped at a click tracker.

Most "resolved" Adzuna links point at `click.appcast.io/...` rather than an
employer. The resolver followed HTTP redirects only; appcast bounces with
JavaScript, so the tracker's own page was the last thing it saw and it recorded
that as the destination. Now that chains are followed properly, resuming from
the tracker usually finishes the journey in one more request.

Run it once after deploy:

    docker compose -f docker-compose.prod.yml exec web python -m app.tasks.links
    docker compose -f docker-compose.prod.yml exec web python -m app.tasks.links --limit 500

or, as a Celery task, `repair_tracker_links.delay()`.
"""

import argparse
import logging

from app.celery_app import celery_app
from app.config import settings
from app.database import SessionLocal
from app.services.link_resolver import retarget_tracker_links

logger = logging.getLogger(__name__)


def _politeness() -> dict:
    return {
        "workers": settings.LINK_RESOLVE_WORKERS,
        "per_host": settings.LINK_RESOLVE_PER_HOST,
        "host_delay": settings.LINK_RESOLVE_HOST_DELAY_MS / 1000.0,
    }


@celery_app.task(
    name="app.tasks.links.repair_tracker_links",
    bind=False,
    soft_time_limit=3600,
    time_limit=3900,
)
def repair_tracker_links(limit: int = 5000) -> dict:
    db = SessionLocal()
    try:
        return retarget_tracker_links(db, limit=limit, **_politeness())
    except Exception as exc:
        logger.error("repair_tracker_links failed: %s", exc)
        db.rollback()
        return {}
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=5000,
                        help="cap on distinct tracker URLs followed (default: 5000)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )

    db = SessionLocal()
    try:
        counts = retarget_tracker_links(db, limit=args.limit, **_politeness())
    finally:
        db.close()

    print("\nTracker link repair")
    print("-------------------")
    for key, value in counts.items():
        print(f"  {key:12} {value}")


if __name__ == "__main__":
    main()
