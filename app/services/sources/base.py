import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

logger = logging.getLogger(__name__)

# Board fetches are network-bound and independent per company, so they run in a
# small thread pool. This is what makes carrying hundreds of discovered company
# slugs per cycle affordable instead of a serial multi-minute crawl.
DEFAULT_BOARD_WORKERS = 8


class SourceUnavailable(Exception):
    """
    The source has told us to stop — bad credentials, quota spent, or a rate
    limit. Retrying the remaining query/location combinations can only produce
    the same answer, so the caller abandons this source for the cycle instead of
    generating dozens of identical errors.
    """


# Statuses that mean "stop asking", as opposed to a transient server fault.
BLOCKING_STATUSES = frozenset({401, 402, 403, 429})


def raise_if_blocked(resp, source: str) -> None:
    """Turn an auth/quota/rate-limit response into SourceUnavailable."""
    if resp.status_code in BLOCKING_STATUSES:
        raise SourceUnavailable(
            f"{source} returned HTTP {resp.status_code}; skipping the rest of "
            f"this source for this cycle"
        )


def fetch_boards_concurrently(
    slugs: list[str],
    fetch_one: Callable[[str], list[dict]],
    label: str,
    workers: int = DEFAULT_BOARD_WORKERS,
) -> list[dict]:
    """
    Run `fetch_one(slug)` for every slug across a thread pool and return the
    flattened jobs, each tagged with the `ats_slug` it came from so the caller
    can attribute per-board yield. A slug that raises is logged and skipped —
    one dead board never costs the rest of the cycle.
    """
    if not slugs:
        return []

    # Log under the ATS's own logger so per-slug failures are attributed to that
    # source rather than to this shared helper (see services.source_diagnostics).
    board_logger = logging.getLogger(f"{__package__}.{label.lower()}")

    def _guarded(slug: str) -> list[dict]:
        try:
            jobs = fetch_one(slug) or []
        except Exception as exc:
            board_logger.error("%s fetch error for slug '%s': %s", label, slug, exc)
            return []
        for job in jobs:
            job.setdefault("ats_slug", slug)
        return jobs

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(slugs)))) as pool:
        results = list(pool.map(_guarded, slugs))

    jobs = [job for board_jobs in results for job in board_jobs]
    board_logger.info("%s: %d jobs across %d companies", label, len(jobs), len(slugs))
    return jobs


def board_workers() -> int:
    from app.config import settings
    return getattr(settings, "ATS_BOARD_FETCH_WORKERS", DEFAULT_BOARD_WORKERS)


def parse_experience_level(title: str, description: str) -> str:
    """
    Infer seniority from job title and description text.

    Returns "entry", "mid", or "senior".
    """
    text = (title + " " + description).lower()

    senior_patterns = [
        r"\bsenior\b", r"\bsr\b", r"\blead\b", r"\bprincipal\b",
        r"\bstaff\b", r"\bdirector\b", r"\bvp\b",
    ]
    if any(re.search(p, text) for p in senior_patterns):
        return "senior"

    entry_patterns = [
        r"\bjunior\b", r"\bjr\b", r"\bentry[\s\-]level\b",
        r"\b0[\s\-]?[-–][\s\-]?[12]\s*years?\b", r"\bnew\s+grad\b",
        r"\bfresh(man|er)?\b",
    ]
    if any(re.search(p, text) for p in entry_patterns):
        return "entry"

    return "mid"
