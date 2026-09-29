"""Run an explicitly opted-in baseline/evidence comparison on a frozen fixture."""
import argparse
import json
from pathlib import Path

from app.services import match_eval, quality_benchmark


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=match_eval.DEFAULT_PATH)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-paid", action="store_true")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--max-calls", type=int, default=100)
    parser.add_argument("--model")
    args = parser.parse_args()
    if not args.allow_paid:
        parser.error("Pass --allow-paid to authorize provider calls. The normal pytest suite is deterministic.")
    labels, profile = match_eval.load(args.path)
    report = quality_benchmark.compare(labels, profile, allow_paid=True, rounds=args.rounds,
                                       max_calls=args.max_calls, model=args.model)
    args.output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"Saved comparison of {len(labels)} frozen labels to {args.output}")


if __name__ == "__main__":
    main()
