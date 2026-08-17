"""
One-time cleanup of descriptions stored before `services.descriptions` existed.

Roughly a third of the table is HTML or double-escaped HTML: the skill filter
greps it, the matcher prompt quotes it, and the document generator writes
bullets out of it. Cleaning is pure reformatting — the same information, in the
shape everything downstream already assumes — so it deliberately does NOT stamp
`description_updated_at`. That stamp means "this posting now says more than it
did", and it drives the "your documents predate a fuller description" nudge;
firing it for 50,000 rows at once would tell the user to rewrite every document
they own for no new information at all.

Run it once after deploy:

    docker compose -f docker-compose.prod.yml exec web python -m app.tasks.descriptions
    docker compose -f docker-compose.prod.yml exec web python -m app.tasks.descriptions --dry-run

or, as a Celery task, `clean_stored_descriptions.delay()`.
"""

import argparse
import logging

from sqlalchemy import or_

from app.celery_app import celery_app
from app.database import SessionLocal
from app.models.job import Job
from app.services.descriptions import clean

logger = logging.getLogger(__name__)

# Same test as section D of scripts/db_samples.sql, so the report and the
# cleanup always disagree about nothing.
_HTML_TAG_SQL = r"<(p|div|ul|ol|li|br|span|strong|b|em|i|h[1-6]|table|tr|td|a)[ >/]"
_ENTITY_SQL = r"&(amp|lt|gt|nbsp|quot|#[0-9]+);"

BATCH_SIZE = 500


def _needs_cleaning():
    """SQLAlchemy filter for descriptions that still look like markup."""
    return or_(
        Job.description.op("~")(_HTML_TAG_SQL),
        Job.description.op("~")(_ENTITY_SQL),
    )


def clean_descriptions(db, batch_size: int = BATCH_SIZE, dry_run: bool = False) -> dict:
    """
    Rewrite every HTML-ish stored description in place, in batches.

    Paginated by id rather than by re-running the same query: a row whose text
    still trips the pattern after cleaning (a posting that genuinely writes
    "&nbsp;" as prose, say) would otherwise be picked up forever, and the loop
    would never end.
    """
    counts = {"scanned": 0, "cleaned": 0, "emptied": 0, "unchanged": 0, "chars_removed": 0}
    last_id = None

    while True:
        query = db.query(Job).filter(_needs_cleaning())
        if last_id is not None:
            query = query.filter(Job.id > last_id)
        batch = query.order_by(Job.id).limit(batch_size).all()
        if not batch:
            break

        for job in batch:
            last_id = job.id
            counts["scanned"] += 1
            before = job.description or ""
            after = clean(before)
            if after == before:
                counts["unchanged"] += 1
                continue
            counts["chars_removed"] += max(0, len(before) - len(after))
            if not dry_run:
                # NULL rather than "": a description that cleaned away to
                # nothing was a block page or empty markup, and "no
                # description" is the state enrichment goes looking for.
                job.description = after or None
            if after:
                counts["cleaned"] += 1
            else:
                counts["emptied"] += 1

        if dry_run:
            db.rollback()
        else:
            db.commit()
        # Not expunged: the commit expires every attribute, which is what
        # releases the description strings — the rows themselves are cheap, and
        # detaching them would break any caller still holding one.
        logger.info(
            "clean_descriptions: %d scanned, %d cleaned, %d emptied so far",
            counts["scanned"], counts["cleaned"], counts["emptied"],
        )

    return counts


@celery_app.task(
    name="app.tasks.descriptions.clean_stored_descriptions",
    bind=False,
    soft_time_limit=3600,
    time_limit=3900,
)
def clean_stored_descriptions(batch_size: int = BATCH_SIZE) -> dict:
    db = SessionLocal()
    try:
        return clean_descriptions(db, batch_size=batch_size)
    except Exception as exc:
        logger.error("clean_stored_descriptions failed: %s", exc)
        db.rollback()
        return {}
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change without writing")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE,
                        help=f"rows per commit (default: {BATCH_SIZE})")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )

    db = SessionLocal()
    try:
        counts = clean_descriptions(
            db, batch_size=args.batch_size, dry_run=args.dry_run
        )
    finally:
        db.close()

    print("\nDescription cleanup")
    print("-------------------")
    for key, value in counts.items():
        print(f"  {key:16} {value}")
    if args.dry_run:
        print("\n(dry run — nothing was written)")


if __name__ == "__main__":
    main()
