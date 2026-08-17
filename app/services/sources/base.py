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


# Browser-ish headers. Several ATS careers pages answer a bare httpx request
# with a redirect to a consent page and a full one with the listing.
LISTING_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def jobs_from_listing(
    url: str,
    source: str,
    slug: str,
    company: str = "",
    timeout: int = 15,
) -> list[dict]:
    """
    Read a careers listing page through the structured data it publishes.

    Every ATS that wants its customers' jobs in Google's job results emits
    `JobPosting` blocks, whether or not it also documents a JSON API. Reading
    those is more durable than reading an undocumented endpoint: the endpoint
    moves and the structured data cannot, because the customer's search
    ranking depends on it.

    Listing pages routinely omit the description from those blocks. That used
    to make this approach useless; it doesn't now, because enrichment fetches
    the full posting from the URL each block carries.
    """
    import httpx

    from app.services.enrichment import json_ld_postings

    resp = httpx.get(url, headers=LISTING_HEADERS, timeout=timeout,
                     follow_redirects=True)
    resp.raise_for_status()

    postings = json_ld_postings(resp.text)
    if not postings:
        # Distinguish "this board has no openings" from "we cannot read this
        # board any more" — they look identical from the job count alone, and
        # the second one is the failure that goes unnoticed for months.
        board_logger = logging.getLogger(f"{__package__}.{source}")
        if len(resp.text) > 2000:
            board_logger.warning(
                "%s/%s: %d bytes returned but no JobPosting structured data "
                "found (the board's markup may have changed)",
                source, slug, len(resp.text),
            )
        return []

    jobs = []
    for posting in postings:
        title = posting["title"]
        description = posting["description"]
        location = posting["location"]
        jobs.append({
            "source": source,
            "source_job_id": _listing_job_id(posting["url"]),
            "title": title,
            "company": company or posting["company"] or slug,
            "location": location,
            "is_remote": "remote" in f"{location} {title}".lower(),
            "url": posting["url"],
            "description": description,
            "experience_level": parse_experience_level(title, description),
            "posted_at": posting["posted_at"],
        })
    return jobs


def _listing_job_id(url: str) -> str | None:
    """The longest number in a posting URL — every ATS puts its id in there."""
    numbers = re.findall(r"\d{3,}", url or "")
    return max(numbers, key=len) if numbers else None


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
