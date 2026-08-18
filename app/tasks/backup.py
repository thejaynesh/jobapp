"""
Take the nightly backup.

Also runnable by hand, which is the only way anybody ever finds out whether
their backup job works before they need it to:

    docker compose -f docker-compose.prod.yml exec web python -m app.tasks.backup

`--verify` re-reads the newest file without taking a new one, and `--restore`
prints the command that puts a backup back.
"""

import argparse
import logging

from app.celery_app import celery_app
from app.database import SessionLocal
from app.services import backups

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.tasks.backup.take_backup",
    bind=False,
    # A dump of a 150,000-row database takes minutes, not seconds, and a soft
    # limit that fired mid-write would leave a partial file — which is why the
    # write goes to a temporary name and is renamed only after verification.
    soft_time_limit=1800,
    time_limit=1860,
)
def take_backup() -> dict:
    db = SessionLocal()
    try:
        return backups.run(db)
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true",
                        help="re-read the newest backup instead of taking one")
    parser.add_argument("--restore", action="store_true",
                        help="print the command that restores the newest backup")
    parser.add_argument("--status", action="store_true",
                        help="what exists on disk, and how old it is")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )

    if args.restore:
        print(
            "\nThis DROPS and recreates every table in the target database.\n"
            "Stop the workers first, or they will write into a half-restored "
            "schema:\n\n"
            "  docker compose -f docker-compose.prod.yml stop worker beat\n\n"
            f"  {backups.restore_command()}\n\n"
            "  docker compose -f docker-compose.prod.yml start worker beat\n"
        )
        return

    if args.verify:
        files = backups.existing()
        if not files:
            print(f"\nNo backups in {backups.directory()}.")
            return
        try:
            size = backups.verify(files[0])
        except Exception as exc:
            print(f"\n{files[0].name} is NOT a usable backup: {exc}")
            return
        print(f"\n{files[0].name} is complete — {size / 1e6:.1f} MB of SQL.")
        return

    db = SessionLocal()
    try:
        if args.status:
            state = backups.status(db)
            print(
                f"\n{state['count']} backup(s) in {state['directory']}, "
                f"{state['total_bytes'] / 1e6:.1f} MB total."
            )
            print(f"Newest: {state['newest'] or 'none'}"
                  + (f" ({state['age_hours']} hours old)"
                     if state["age_hours"] is not None else ""))
            if state["last_error"]:
                print(f"Last attempt FAILED: {state['last_error']}")
            if state["stale"]:
                print(
                    "\nThis is stale. Backups are supposed to run every "
                    f"{state['interval_hours']} hours — check that beat is "
                    "running and that pg_dump is installed in the image."
                )
            return

        result = backups.run(db)
    finally:
        db.close()

    if result.get("ok"):
        print(
            f"\nWrote {result['file']} — {result['bytes'] / 1e6:.1f} MB "
            f"compressed, verified, keeping {result['kept']}."
        )
        if result["removed"]:
            print(f"Removed {len(result['removed'])} older backup(s).")
    else:
        print(f"\nBackup failed: {result.get('error') or result.get('detail')}")


if __name__ == "__main__":
    main()
