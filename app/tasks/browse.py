"""
Keeping the browser's queue topped up, without a button being pressed.

The crawl was manual only, which made it a thing the user remembers rather than
a thing the system does — and the whole argument for browsing is that it reaches
sources no API can. A source that only runs when someone thinks of it is not
really a source.

This is a backstop, not a second fetch cycle, and the distinction shapes it. The
queue drains at a person's pace — one page every twenty seconds — so the useful
question is never "should we fetch more" but "has the browser run out of work".
It tops up when the queue is nearly empty and otherwise does nothing at all,
which means it can run often and stay cheap.

It also declines to queue anything when no agent has polled recently. Work
queued for a laptop that is shut fills the queue with tasks that expire unread
and hides the real backlog behind them, and unlike a fetch there is nothing the
server can do about it alone.
"""

import argparse
import logging

from app.celery_app import celery_app
from app.database import SessionLocal

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.tasks.browse.top_up_browsing",
    bind=False,
    soft_time_limit=120,
    time_limit=180,
)
def top_up_browsing() -> dict:
    """
    Queue more pages for the browser if it has run out.

    Short time limits on purpose: this only ever writes a handful of rows. If
    it is taking two minutes something is wrong that a longer limit would only
    hide.
    """
    from app.models.profile import Profile
    from app.services import browse_plan

    db = SessionLocal()
    try:
        profile = db.query(Profile).first()
        outcome = browse_plan.scheduled_crawl(db, profile.data if profile else None)
        if outcome.get("queued"):
            logger.info(
                "top_up_browsing: queued %d page(s) — %s",
                outcome["queued"], outcome.get("kind", "unknown"),
            )
        elif outcome.get("skipped"):
            logger.debug("top_up_browsing: %s", outcome["skipped"])
        return outcome
    except Exception as exc:
        logger.error("top_up_browsing: failed: %s", exc)
        db.rollback()
        return {"queued": 0, "error": str(exc)[:200]}
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true",
                        help="show the queue and whether an agent is around")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.status:
        from app.services import browse_plan

        db = SessionLocal()
        try:
            state = browse_plan.status(db)
            print(f"enabled:      {state['enabled']}")
            print(f"waiting:      {state['waiting']} page(s)")
            print(f"eta:          {state['eta_minutes']} min")
            print(f"agent seen:   {browse_plan.agent_seen_recently(db)}")
        finally:
            db.close()
        return

    print(top_up_browsing())


if __name__ == "__main__":
    main()
