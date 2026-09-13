"""
Why a board that returns a page yields no jobs from it.

Four adapters report the same sentence on every cycle — "N bytes returned but
no JobPosting structured data found (the board's markup may have changed)" —
and Y Combinator says it about 277 kilobytes. That message covers two entirely
different failures and cannot tell them apart:

  * the page carries no `JobPosting` blocks at all, because it renders its jobs
    in JavaScript and there is nothing for a fetcher to read; or
  * the blocks are right there, and `json_ld_postings` discards them because it
    requires both a title *and* a url and the listing page omits one.

The first is unfixable by parsing and needs the browser tier. The second is a
few lines. Guessing between them is how an adapter stays broken for months,
which is what the identical wording has been hiding.

So this fetches each board the way its adapter does and counts the stages:
bytes, ld+json blocks, JobPosting nodes, and how many of those carry a title
and a url. The stage where the number falls to zero is the answer.

Read-only. Fetches pages; stores nothing.

    docker compose -f docker-compose.prod.yml run --rm web python scripts/why_no_jobs.py
    docker compose -f docker-compose.prod.yml run --rm web python scripts/why_no_jobs.py ycombinator
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# One real, currently-failing URL per adapter, taken from the slugs in
# `fetch_source_runs.errors` so this reproduces the actual failure rather than
# a hypothetical one.
BOARDS = {
    "ycombinator": "https://www.ycombinator.com/jobs/role/software-engineer",
    "teamtailor": "https://virtasant.teamtailor.com/jobs",
    "jobvite": "https://jobs.jobvite.com/aarete/search",
    "icims": "https://corporate-crashchampions.icims.com/jobs/search"
             "?ss=1&searchRelation=keyword_all",
    "hiringcafe": "https://hiring.cafe/api/search-jobs",
}

SHOW_KEYS = 16


def main():
    import httpx

    from app.services.enrichment import (
        _is_job_posting, _ld_blocks, _ld_text, _walk_ld, json_ld_postings,
    )
    from app.services.sources.base import LISTING_HEADERS

    only = sys.argv[1] if len(sys.argv) > 1 else ""
    boards = {k: v for k, v in BOARDS.items() if not only or k == only}
    if not boards:
        sys.exit(f"Unknown board {only!r}. Known: {', '.join(BOARDS)}")

    print(f"{'board':<14} {'http':>5} {'bytes':>9} {'ld+json':>8} "
          f"{'JobPosting':>11} {'has title':>10} {'has url':>8} {'read':>6}")
    print("-" * 88)

    samples = {}
    for name, url in boards.items():
        try:
            resp = httpx.get(url, headers=LISTING_HEADERS, timeout=25,
                             follow_redirects=True)
        except Exception as exc:
            print(f"{name:<14} {'---':>5} {str(exc)[:60]}")
            continue

        html = resp.text
        blocks = list(_ld_blocks(html))
        nodes = [n for data in blocks for n in _walk_ld(data)
                 if isinstance(n, dict) and _is_job_posting(n)]
        titled = [n for n in nodes if _ld_text(n.get("title"))]
        linked = [n for n in nodes
                  if _ld_text(n.get("url")) or _ld_text(n.get("sameAs"))]
        read = json_ld_postings(html)

        print(f"{name:<14} {resp.status_code:>5} {len(html):>9,} "
              f"{len(blocks):>8} {len(nodes):>11} {len(titled):>10} "
              f"{len(linked):>8} {len(read):>6}")
        if nodes:
            samples[name] = nodes[0]

    print()
    print("Read the row left to right and stop where it hits zero.")
    print("  ld+json 0      the page has no structured data — it renders its")
    print("                 jobs in JavaScript, and no parser change helps.")
    print("                 That board belongs to the browser tier.")
    print("  JobPosting 0   there is structured data but none of it is a job")
    print("                 (breadcrumbs, the organisation, a search box).")
    print("  has url 0      the blocks are jobs and we are throwing them away")
    print("                 for want of a field the listing page omits. Cheap.")

    for name, node in samples.items():
        print()
        print(f"--- {name}: the first JobPosting node's keys " + "-" * 30)
        for key in list(node)[:SHOW_KEYS]:
            value = node[key]
            if isinstance(value, str):
                value = value[:70]
            elif isinstance(value, (dict, list)):
                value = f"<{type(value).__name__} of {len(value)}: " \
                        f"{json.dumps(value)[:60]}>"
            print(f"    {key:<22} {value!r}")


if __name__ == "__main__":
    main()
