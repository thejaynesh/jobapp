"""
Building the corpus for a company, on a trigger.

Two triggers by design, which is the rule the design doc applies to every
automatic behaviour in the system: automation decides when this *usually*
happens, and the user decides when it happens *now*. So this is one task,
callable from the mailbox poller when an interview invite arrives and from a
button on an application.
"""

import logging

from app.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.interview.research_company", soft_time_limit=300)
def research_company(company: str, only: list[str] | None = None) -> dict:
    """Fetch every free source for one company and store what is usable."""
    from app.database import SessionLocal
    from app.services.interview_corpus import ingest
    from app.services.interview_sources import fetch_all

    if not (company or "").strip():
        return {"error": "no company"}

    db = SessionLocal()
    try:
        outcome = fetch_all(company, set(only) if only else None)
        counts = ingest(db, outcome["reports"])
        # Per-source counts are logged rather than only summed: a source that
        # has started returning nothing looks exactly like a company nobody has
        # written about, and those need different responses.
        logger.info(
            "research_company(%s): stored %d (%d dup, %d undated) — sources %s",
            company, counts["stored"], counts["duplicate"], counts["undated"],
            outcome["sources"],
        )
        return {"company": company, **counts, "sources": outcome["sources"]}
    except Exception as exc:
        logger.error("research_company(%s) failed: %s", company, exc)
        return {"company": company, "error": str(exc)}
    finally:
        db.close()
