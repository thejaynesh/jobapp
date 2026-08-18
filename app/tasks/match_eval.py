"""
Run the match-quality check.

Build the fixture once from decisions you have already made:

    docker compose -f docker-compose.prod.yml exec web python -m app.tasks.match_eval --build

then run it before and after any prompt or provider change:

    docker compose -f docker-compose.prod.yml exec web python -m app.tasks.match_eval

With no --model it scores through whatever actually scores your jobs — the
provider MATCH_PRIMARY selects. That is the number worth comparing before and
after, because an agreement figure measured against a provider the pipeline is
not using is not evidence about the pipeline.

--model asks a specific NVIDIA NIM model instead, whatever the primary is:

    docker compose -f docker-compose.prod.yml exec web python -m app.tasks.match_eval --model meta/llama-3.3-70b-instruct

The fixture is a plain JSON file. Edit it, add jobs by hand, delete the ones
you have changed your mind about — it is meant to be read.
"""

import argparse
import logging
from pathlib import Path

from app.celery_app import celery_app
from app.database import SessionLocal
from app.services import match_eval

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.tasks.match_eval.check_match_quality",
    bind=False,
    soft_time_limit=1500,
    time_limit=1740,
)
def check_match_quality(model: str | None = None, path: str | None = None) -> dict:
    """Score the labelled fixture through the current prompt and model."""
    try:
        labels, profile = match_eval.load(
            Path(path) if path else match_eval.DEFAULT_PATH
        )
    except (FileNotFoundError, ValueError) as exc:
        logger.warning("check_match_quality: %s", exc)
        return {"error": str(exc)}

    result = match_eval.run(labels, profile, model=model)
    logger.info(
        "match quality: %.1f%% agreement (%d/%d), %d false rejects, %d false accepts",
        result["agreement"], result["agreed"], result["scored"],
        result["false_rejects"], result["false_accepts"],
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", action="store_true",
                        help="build the label file from your own past decisions")
    parser.add_argument("--per-side", type=int, default=25,
                        help="when building: labels per class (default: 25)")
    parser.add_argument("--model", default=None,
                        help="score with this NVIDIA NIM model instead of the "
                             "provider MATCH_PRIMARY selects")
    parser.add_argument("--path", default=str(match_eval.DEFAULT_PATH),
                        help=f"label file (default: {match_eval.DEFAULT_PATH})")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    path = Path(args.path)

    db = SessionLocal()
    try:
        if args.build:
            labels = match_eval.build_labels(db, limit_per_side=args.per_side)
            if not labels:
                print(
                    "\nNothing to label yet. This reads jobs you applied to and "
                    "jobs you marked 'not interested' — once there are some of "
                    "each, run this again. You can also write the file by hand."
                )
                return
            match_eval.save(labels, match_eval.profile_snapshot(db), path)
            good = sum(1 for lab in labels if lab.verdict == match_eval.GOOD)
            print(
                f"\nWrote {len(labels)} labels to {path} "
                f"({good} you wanted, {len(labels) - good} you rejected).\n"
                f"Read it, fix anything it got wrong, then run this without "
                f"--build."
            )
            return

        labels, profile = match_eval.load(path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"\n{exc}")
        return
    finally:
        db.close()

    print(match_eval.format_report(match_eval.run(labels, profile, model=args.model)))


if __name__ == "__main__":
    main()
