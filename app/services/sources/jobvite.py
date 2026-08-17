"""
Jobvite-hosted career sites.

Jobvite serves customer boards from `jobs.jobvite.com/<slug>`, and publishes
`JobPosting` structured data on them. Read the same way as iCIMS and
Teamtailor.

Note the neighbouring hazard: `click.jobvite.com` is Jobvite's *click tracker*,
not a board, and the link resolver treats it as a middleman to follow through
rather than a destination. Only `jobs.jobvite.com` is a board.
"""

import logging

from app.services.sources.base import (
    board_workers,
    fetch_boards_concurrently,
    jobs_from_listing,
)

logger = logging.getLogger(__name__)

# The `/search` route is the one that renders every opening; the bare slug
# lands on a marketing page that lists only a handful.
_LISTING = "https://jobs.jobvite.com/{slug}/search"
_FALLBACK = "https://jobs.jobvite.com/{slug}"


def fetch(company_slugs: list[str]) -> list[dict]:
    """Fetch jobs from Jobvite career sites (no key required)."""

    def _fetch_one(slug: str) -> list[dict]:
        jobs = jobs_from_listing(_LISTING.format(slug=slug), "jobvite", slug)
        if jobs:
            return jobs
        return jobs_from_listing(_FALLBACK.format(slug=slug), "jobvite", slug)

    return fetch_boards_concurrently(
        company_slugs, _fetch_one, "Jobvite", board_workers()
    )
