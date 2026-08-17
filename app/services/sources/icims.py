"""
iCIMS-hosted career portals.

iCIMS has the largest enterprise footprint of any ATS here and the least
public API surface: its portals are per-customer, its endpoints are
undocumented, and the query parameters differ between installations. What every
portal does have is `JobPosting` structured data on its search page, because
its customers' openings appearing in Google's job results is the point of the
product.

So this reads that rather than an endpoint. It is the more durable of the two
reads: the endpoint can move, and the structured data cannot without costing
the customer their search ranking.

Search pages carry no descriptions. Enrichment fetches them from the posting
URLs afterwards, which is exactly the case it was built for.
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
_SEARCH_URL = "https://{host}/jobs/search?ss=1&searchRelation=keyword_all"


def _hosts(slug: str) -> list[str]:
    if ".icims.com" in slug:
        return [slug.replace("https://", "").replace("http://", "").strip("/")]
    return [f"{slug}.icims.com", f"careers-{slug}.icims.com"]


def fetch(company_slugs: list[str]) -> list[dict]:
    """Fetch jobs from iCIMS portals via their published structured data."""

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
