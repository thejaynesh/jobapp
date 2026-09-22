"""
Teamtailor career sites.

Teamtailor's documented API needs a per-customer key, which we will never have.
Its public career sites do not: `<company>.teamtailor.com/jobs` lists the
openings as HTML cards. The shared listing reader supports those cards and
JobPosting structured data when available.
"""

import logging

from app.services.sources.base import (
    board_workers,
    fetch_boards_concurrently,
    jobs_from_listing,
)

logger = logging.getLogger(__name__)

_LISTING = "https://{slug}.teamtailor.com/jobs"


def fetch(company_slugs: list[str]) -> list[dict]:
    """Fetch jobs from Teamtailor career sites (no key required)."""

    def _fetch_one(slug: str) -> list[dict]:
        return jobs_from_listing(_LISTING.format(slug=slug), "teamtailor", slug)

    return fetch_boards_concurrently(
        company_slugs, _fetch_one, "Teamtailor", board_workers()
    )
