"""
Move settled rejections out of the hot table.

Runnable by hand, which is how the first pass should be done — the backlog is
six figures, and watching one bounded batch go through before letting it run on
a timer is cheaper than reading the code twice:

    docker compose -f docker-compose.prod.yml exec web python -m app.tasks.archive --dry-run
    docker compose -f docker-compose.prod.yml exec web python -m app.tasks.archive
"""

import argparse
import logging

from app.celery_app import celery_app
from app.database import SessionLocal
from app.services import archive

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.tasks.archive.archive_old_jobs",
    bind=False,
    soft_time_limit=900,
    time_limit=960,
)
def archive_old_jobs() -> dict:
    db = SessionLocal()
    try:
        return archive.archive(db)
    except Exception as exc:
        db.rollback()
        logger.error("archive_old_jobs failed: %s", exc)
        return {"archived": 0, "error": str(exc)}
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="say what would go, and move nothing")
    parser.add_argument("--days", type=int, default=None,
                        help="archive rejections older than this many days")
    parser.add_argument("--limit", type=int, default=None,
                        help="how many to move in this pass")
    parser.add_argument("--all", action="store_true",
                        help="keep going until nothing is left to archive")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )

    db = SessionLocal()
    try:
        if args.dry_run:
            rows = archive.candidates(db, days=args.days, limit=args.limit)
            total = archive.remaining(db, days=args.days)
            print(f"\n{total:,} job(s) eligible; this pass would move {len(rows):,}.")
            for job in rows[:10]:
                print(f"  {job.fetched_at:%Y-%m-%d}  {job.filter_reason or '?':<16} "
                      f"{(job.title or '')[:44]:<44} {(job.company or '')[:24]}")
            if len(rows) > 10:
                print(f"  … and {len(rows) - 10:,} more")
            return

        while True:
            result = archive.archive(db, days=args.days, limit=args.limit)
            if not result.get("enabled", True):
                print("\nArchiving is switched off (ARCHIVE_ENABLED).")
                return
            print(
                f"Moved {result['archived']:,}"
                + (f" ({result['skipped']:,} already tombstoned)"
                   if result["skipped"] else "")
                + f"; {result.get('remaining', 0):,} still eligible."
            )
            if not args.all or not result["archived"]:
                break
    finally:
        db.close()


if __name__ == "__main__":
    main()
