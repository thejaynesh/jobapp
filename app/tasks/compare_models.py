"""
Compare matching models on your own stored jobs.

    docker compose -f docker-compose.prod.yml exec web python -m app.tasks.compare_models
    ... --models meta/llama-3.3-70b-instruct,qwen/qwen3-next-80b-a3b-instruct
    ... --limit 15

Scores the same jobs through each model and prints them side by side, with the
count of replies the parser could not read — the number that decides whether a
model is usable here at all.
"""

import argparse
import logging

from app.config import settings
from app.database import SessionLocal
from app.services.model_compare import compare_models, format_report

logger = logging.getLogger(__name__)

# The current model plus the strongest same-shape alternative on NIM.
DEFAULT_MODELS = "meta/llama-3.1-70b-instruct,meta/llama-3.3-70b-instruct"


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
