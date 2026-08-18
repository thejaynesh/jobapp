"""
The provider check, on a worker.

It makes real calls to real providers, which take real seconds — more when a
single-slot provider is mid-call and this one has to queue behind it. The proxy
in front of the app gives an upstream sixty seconds before returning a 504, so
running this inline in the request that asks for it means the answer arrives as
a gateway error often enough to be useless.
"""

import logging
from datetime import datetime, timezone

from app.celery_app import celery_app
from app.database import SessionLocal

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.providers.prune_llm_log", bind=False)
def prune_llm_log() -> dict:
    """
    Keep the LLM log to its retention limit.

    Prompts carry whole job descriptions, so this table grows faster than
    anything else in the schema — and a diagnostic that fills the disk stops
    being a diagnostic.
    """
    from app.services.llm_log import prune

    db = SessionLocal()
    try:
        return {"removed": prune(db)}
    except Exception as exc:
        logger.error("prune_llm_log failed: %s", exc)
        return {"removed": 0, "error": str(exc)}
    finally:
        db.close()


@celery_app.task(name="app.tasks.providers.prune_agent_history", bind=False)
def prune_agent_history() -> dict:
    """
    Keep the browser agent's two histories to their limits.

    Two tables with opposite shapes, which is why they are pruned differently.
    `agent_events` rows are a kind, a host and a few counts, so tens of
    thousands cost nothing and the questions they answer are about weeks —
    capped by row count. `browser_tasks` rows carry the page the browser
    brought back, which is the large part, and nothing has ever pruned them —
    capped by age.
    """
    from app.services.agent_events import prune as prune_events
    from app.services.browser_tasks import prune as prune_tasks

    db = SessionLocal()
    try:
        return {"events": prune_events(db), "tasks": prune_tasks(db)}
    except Exception as exc:
        db.rollback()
        logger.error("prune_agent_history failed: %s", exc)
        return {"events": 0, "tasks": 0, "error": str(exc)}
    finally:
        db.close()


@celery_app.task(
    name="app.tasks.providers.run_provider_check",
    bind=False,
    soft_time_limit=240,
    time_limit=270,
)
def run_provider_check() -> dict:
    from app.services.provider_check import _now, check_providers, load_state, store_state

    db = SessionLocal()
    try:
        previous = load_state(db) or {}
        store_state(db, {"status": "running", "results": [],
                         "queued_at": previous.get("queued_at"),
                         "started_at": _now()})
        results = check_providers()
        store_state(db, {"status": "done", "results": results,
                         "queued_at": previous.get("queued_at"),
                         "started_at": previous.get("started_at") or _now(),
                         "finished_at": _now()})
        logger.info(
            "provider check — %s",
            ", ".join(f"{r['name']}:{'ok' if r['ok'] else 'failed'}" for r in results),
        )
        return {"checked": len(results),
                "ok": sum(1 for r in results if r["ok"])}
    except Exception as exc:
        logger.error("provider check failed: %s", exc)
        # Stored, not just logged: a check that leaves the panel spinning
        # forever is the exact failure this whole feature exists to expose.
        try:
            store_state(db, {"status": "failed", "results": [],
                             "error": str(exc)[:300],
                             "finished_at": datetime.now(timezone.utc).isoformat()})
        except Exception:
            logger.error("provider check: could not record the failure either")
        return {"checked": 0, "ok": 0, "error": str(exc)}
    finally:
        db.close()
