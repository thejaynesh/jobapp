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
    from app.services import commoncrawl, company_boards
    from app.services.tunables import value

    db = SessionLocal()
    try:
        profile = db.query(Profile).first()
        data = profile.data if profile else {}
        report: dict = {"ok": True}
        # Every career site of the Workday tenants already registered. Cheap
        # after the first pass — only tenants not read this month are asked —
        # so it runs on every tick rather than waiting for the walk below.
        if value(data, "workday_site_discovery"):
            try:
                report["workday_sites"] = company_boards.expand_workday_sites(db)
                db.commit()
            except Exception as exc:
                db.rollback()
                logger.warning("discovery: Workday site expansion failed: %s", exc)
        # Probe boards waiting to be confirmed. The board cycle does this too,
        # but only every few hours; a list of tens of thousands would wait
        # weeks for it alone.
        from app.config import settings

        per_hour = int(value(data, "ats_board_validate_hourly") or 0)
        if per_hour > 0 and settings.ATS_BOARD_REGISTRY and settings.ATS_BOARD_VALIDATION:
            try:
                report["validated"] = company_boards.validate_pending(
                    db, limit=per_hour,
                    workers=int(value(data, "ats_board_fetch_workers") or 8))
                db.commit()
            except Exception as exc:
                db.rollback()
                logger.warning("discovery: board validation failed: %s", exc)
        if not force and not value(data, "commoncrawl_enabled"):
            return {**report, "skipped": True, "detail": "switched off"}
        return {**report, **commoncrawl.run(
            db,
            pages_per_run=value(data, "commoncrawl_pages_per_run"),
            interval_hours=value(data, "commoncrawl_interval_hours"),
            force=force,
        )}
    finally:
        db.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    print(discover_boards(force=True))
