"""
Company-board discovery that does not start from a posting we already hold.

The hourly tick asks whether a walk of Common Crawl's index is due by the
settings page's interval, the way the backup does, so changing the interval
bites within the hour. Runnable by hand:

    docker compose -f docker-compose.prod.yml exec worker python -m app.tasks.discovery
"""

import logging

from app.celery_app import celery_app
from app.database import SessionLocal

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.tasks.discovery.discover_boards",
    bind=False,
    # A run reads a budget of index pages, each several seconds, with pauses
    # between them; generous so a slow index day finishes rather than dies.
    soft_time_limit=1800,
    time_limit=1860,
)
def discover_boards(force: bool = False) -> dict:
    from app.models.profile import Profile
    from app.services import commoncrawl
    from app.services.tunables import value

    db = SessionLocal()
    try:
        profile = db.query(Profile).first()
        data = profile.data if profile else {}
        if not force and not value(data, "commoncrawl_enabled"):
            return {"ok": True, "skipped": True, "detail": "switched off"}
        return commoncrawl.run(
            db,
            pages_per_run=value(data, "commoncrawl_pages_per_run"),
            interval_hours=value(data, "commoncrawl_interval_hours"),
            force=force,
        )
    finally:
        db.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    print(discover_boards(force=True))
