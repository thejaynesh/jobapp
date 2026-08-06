"""
Compare matching models on your own stored jobs.

Normally you'd start this from the "Compare matching models" panel on /runs,
which queues `run_comparison` and polls for the result. The command line below
does the same thing without a browser:

    docker compose -f docker-compose.prod.yml exec web python -m app.tasks.compare_models
    ... --models meta/llama-3.3-70b-instruct,qwen/qwen3-next-80b-a3b-instruct
    ... --limit 15

Scores the same jobs through each model and prints them side by side, with the
count of replies the parser could not read — the number that decides whether a
model is usable here at all.
"""

import argparse
import copy
import logging
from datetime import datetime, timezone

from app.celery_app import celery_app
from app.config import settings
from app.database import SessionLocal
from app.services.fetch_lock import COMPARE_LOCK_KEY, acquire, release
from app.services.model_compare import compare_models, format_report, report_dict

logger = logging.getLogger(__name__)

# The current model plus the strongest same-shape alternative on NIM.
DEFAULT_MODELS = "meta/llama-3.1-70b-instruct,meta/llama-3.3-70b-instruct"

# A comparison is minutes of LLM calls; the page that starts it is rarely the
# page that reads it, so the result lives on the profile.
RESULT_KEY = "model_comparison"


def _store(db, payload: dict) -> None:
    from app.models.profile import Profile

    profile = db.query(Profile).first()
    if profile is None:
        return
    data = copy.deepcopy(profile.data)
    data[RESULT_KEY] = payload
    profile.data = data
    db.commit()


@celery_app.task(name="app.tasks.compare_models.run_comparison", bind=True, max_retries=0)
def run_comparison(self, models: list[str], limit: int = 10,
                   pace: float = 1.5) -> dict:
    """
    Score the same jobs through each model and leave the result on the profile.

    Locked separately from fetching: the two don't conflict, but two
    comparisons at once would double the LLM spend for no extra information.
    """
    if not acquire(key=COMPARE_LOCK_KEY, ttl=1800):
        logger.warning("run_comparison: a comparison is already running")
        return {"status": "already running"}

    db = SessionLocal()
    started = datetime.now(timezone.utc).isoformat()
    try:
        jobs, results = compare_models(db, models, limit, pace)
        payload = report_dict(jobs, results, settings.MIN_MATCH_SCORE)
        payload.update({"at": started, "status": "done"})
        if not jobs:
            payload["status"] = "no jobs"
        _store(db, payload)
        return {"status": payload["status"], "models": models}
    except Exception as exc:
        logger.error("run_comparison failed: %s", exc)
        try:
            _store(db, {"at": started, "status": "failed", "error": str(exc)[:300],
                        "models": models, "rows": [], "summary": [], "flips": []})
        except Exception:
            db.rollback()
        return {"status": "failed", "error": str(exc)}
    finally:
        db.close()
        release(key=COMPARE_LOCK_KEY)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", default=DEFAULT_MODELS,
                        help=f"comma-separated model ids (default: {DEFAULT_MODELS})")
    parser.add_argument("--limit", type=int, default=10,
                        help="how many stored jobs to score (default: 10)")
    parser.add_argument("--pace", type=float, default=0.0,
                        help="seconds to wait between calls, to respect the RPM cap")
    parser.add_argument("--threshold", type=int, default=None,
                        help="match threshold for the verdict-flip report")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    threshold = args.threshold if args.threshold is not None else settings.MIN_MATCH_SCORE

    db = SessionLocal()
    try:
        jobs, results = compare_models(db, models, args.limit, args.pace)
    finally:
        db.close()

    print(format_report(jobs, results, threshold))


if __name__ == "__main__":
    main()
