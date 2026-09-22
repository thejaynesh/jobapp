"""iCIMS career portals: read structured data or cards from the inner listing.

The outer search page can be an iframe wrapper. Listings without descriptions
are enriched from their posting URLs later.
"""

import logging

from app.services.sources.base import (
    board_workers,
    fetch_boards_concurrently,
    jobs_from_listing,
)

logger = logging.getLogger(__name__)

# Two host shapes in the wild: `<slug>.icims.com` and `careers-<slug>.icims.com`.
# A slug may be configured either way; both are tried.
_SEARCH_URL = "https://{host}/jobs/search?ss=1&searchRelation=keyword_all&in_iframe=1"


def _hosts(slug: str) -> list[str]:
    if ".icims.com" in slug:
        return [slug.replace("https://", "").replace("http://", "").strip("/")]
    return [f"{slug}.icims.com", f"careers-{slug}.icims.com"]


def fetch(company_slugs: list[str]) -> list[dict]:
    """Fetch jobs from the readable iCIMS listing."""

    def _fetch_one(slug: str) -> list[dict]:
        last_error: Exception | None = None
        for host in _hosts(slug):
            try:
                jobs = jobs_from_listing(
                    _SEARCH_URL.format(host=host), "icims", slug, timeout=20
                )
            except Exception as exc:
                last_error = exc
                continue
            if jobs:
                return jobs
        if last_error is not None:
            raise last_error
        return []

    return fetch_boards_concurrently(company_slugs, _fetch_one, "iCIMS", board_workers())
