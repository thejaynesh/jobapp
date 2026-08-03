"""
One-off backfill of the company board registry from the existing jobs table.

Run it once after the registry ships:

    docker compose -f docker-compose.prod.yml exec web python -m app.tasks.backfill
    docker compose -f docker-compose.prod.yml exec web python -m app.tasks.backfill --dry-run
    docker compose -f docker-compose.prod.yml exec web python -m app.tasks.backfill --slugs-only

or, as a Celery task, `backfill_boards.delay()`.
"""

import argparse
import logging

from app.celery_app import celery_app
from app.database import SessionLocal
from app.services.board_backfill import backfill_boards

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.backfill.backfill_boards", bind=True, max_retries=0)
def backfill_boards_task(
    self,
    resolve_links: bool = True,
    sniff_sites: bool = True,
    max_links: int = 2000,
    max_hosts: int = 500,
) -> dict:
    db = SessionLocal()
    try:
        report = backfill_boards(
            db,
            resolve_links=resolve_links,
            sniff_sites=sniff_sites,
            max_links=max_links,
            max_hosts=max_hosts,
        )
        return report.as_dict()
    except Exception as exc:
        logger.error("backfill_boards task raised unexpectedly: %s", exc)
        db.rollback()
        return {}
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be found without writing or fetching")
    parser.add_argument("--slugs-only", action="store_true",
                        help="offline pass: mine stored text, make no outbound requests")
    parser.add_argument("--max-links", type=int, default=2000,
                        help="cap on aggregator redirects followed (default: 2000)")
    parser.add_argument("--max-hosts", type=int, default=500,
                        help="cap on careers sites sniffed (default: 500)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )

    db = SessionLocal()
    try:
        report = backfill_boards(
            db,
            resolve_links=not args.slugs_only,
            sniff_sites=not args.slugs_only,
            max_links=args.max_links,
            max_hosts=args.max_hosts,
            dry_run=args.dry_run,
        )
    finally:
        db.close()

    print("\nBackfill report")
    print("---------------")
    for key, value in report.as_dict().items():
        print(f"  {key:24} {value}")
    if args.dry_run:
        print("\n(dry run — nothing was written)")


if __name__ == "__main__":
    main()
