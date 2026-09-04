"""
Run every stored payload back through the reader, and say why it found nothing.

`harvest_samples` keeps the responses the shape walker could not read — that is
what the table is for. So the question "why does the walker find zero jobs in
3,000 Dice, Handshake and LinkedIn payloads while it finds 1,211 in a hundred
Tsenta ones" does not need theorising about. The payloads are right there.

Read-only. Stores nothing, changes nothing.

    docker compose -f docker-compose.prod.yml run --rm web python scripts/replay_samples.py
    docker compose -f docker-compose.prod.yml run --rm web python scripts/replay_samples.py app.joinhandshake.com

What it reports, per host, is the reader's own three tests taken apart:

  titles       objects with something title-shaped in them
  +company     ...of which also name a company in the *same* object
  +id/url      ...of which also carry an id or a link
  jobs         what `extract_jobs` actually returned

A host with titles and no `+company` is the reference-shaped payload problem:
the company is a URN or an id resolved elsewhere, and the walker's rule that
all three must sit in one object cannot see it.

A host with no titles at all is either a payload with no jobs in it, or a walk
that ran out of budget before reaching them — which the `walk` line below
distinguishes, because that one has been a real ceiling: 20,000 nodes and depth
12 were chosen against payloads a tenth the size of a modern GraphQL response.
"""

import collections
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

SHOW_KEYS = 14


def _measure(payload):
    """How big and how deep this payload is, ignoring the reader's budget."""
    nodes = 0
    deepest = 0
    stack = [(payload, 0)]
    while stack:
        node, depth = stack.pop()
        nodes += 1
        deepest = max(deepest, depth)
        if isinstance(node, dict):
            stack.extend((v, depth + 1) for v in node.values())
        elif isinstance(node, list):
            stack.extend((v, depth + 1) for v in node)
    return nodes, deepest


def _titles(payload, harvest):
    """
    Every object with a title, and how far it got through the other two tests.

    Walked here rather than through `harvest._walk`, deliberately: the point is
    to see what is in the payload, not what the reader's budget lets it see.
    """
    found = []
    stack = [(payload, 0)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict):
            if harvest._first(node, harvest._TITLE_KEYS):
                found.append((node, depth))
            stack.extend((v, depth + 1) for v in node.values())
        elif isinstance(node, list):
            stack.extend((v, depth + 1) for v in node)
    return found


def main():
    from app.database import SessionLocal
    from app.models.harvest_recipe import HarvestSample
    from app.services import harvest

    only = sys.argv[1] if len(sys.argv) > 1 else ""
    db = SessionLocal()
    try:

        query = db.query(HarvestSample).order_by(HarvestSample.bytes.desc())
        if only:
            query = query.filter(HarvestSample.host == only)
        samples = query.limit(400).all()
        if not samples:
            sys.exit(f"No samples stored{f' for {only}' if only else ''}.")

        print(f"reader budget: {harvest._MAX_NODES:,} nodes, depth "
              f"{harvest._MAX_DEPTH}")
        print()
        print(f"{'host':<38} {'samples':>7} {'titles':>7} {'+company':>9} "
              f"{'+id/url':>8} {'jobs':>6} {'over budget':>12} {'max depth':>10}")
        print("-" * 106)

        per_host = collections.defaultdict(lambda: {
            "samples": 0, "titles": 0, "company": 0, "identified": 0,
            "jobs": 0, "over": 0, "depth": 0,
        })
        keys_seen = collections.defaultdict(collections.Counter)

        for sample in samples:
            row = per_host[sample.host]
            row["samples"] += 1
            nodes, deepest = _measure(sample.payload)
            row["depth"] = max(row["depth"], deepest)
            if nodes > harvest._MAX_NODES or deepest > harvest._MAX_DEPTH:
                row["over"] += 1

            for node, _depth in _titles(sample.payload, harvest):
                row["titles"] += 1
                has_company = bool(harvest._first(node, harvest._COMPANY_KEYS))
                identified = bool(harvest._job_id(node)
                                  or harvest._first(node, harvest._URL_KEYS))
                if has_company:
                    row["company"] += 1
                if has_company and identified:
                    row["identified"] += 1
                if not has_company:
                    # The keys a title-bearing object actually has is the whole
                    # answer when the company is missing: it says what to add
                    # to _COMPANY_KEYS, or that the company is not in there at
                    # all and has to come from an ancestor.
                    keys_seen[sample.host].update(k for k in node if k != "title")

            row["jobs"] += len(harvest.extract_jobs(sample.payload))

        for host, row in sorted(per_host.items(), key=lambda kv: -kv[1]["titles"]):
            print(f"{host:<38} {row['samples']:>7} {row['titles']:>7} "
                  f"{row['company']:>9} {row['identified']:>8} {row['jobs']:>6} "
                  f"{row['over']:>12} {row['depth']:>10}")

        for host, counter in sorted(keys_seen.items(),
                                    key=lambda kv: -sum(kv[1].values())):
            if not counter:
                continue
            print()
            print(f"--- {host}: keys beside a title that had no company "
                  + "-" * max(0, 40 - len(host)))
            for key, count in counter.most_common(SHOW_KEYS):
                print(f"    {count:>6}  {key}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
