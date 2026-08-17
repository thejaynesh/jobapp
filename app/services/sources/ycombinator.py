"""
Y Combinator's public jobs board.

Work at a Startup's own search needs an account; the ycombinator.com job pages
do not, and they publish `JobPosting` structured data because YC wants those
roles in Google's job results. Same read as the iCIMS/Teamtailor adapters, for
the same reason.

Scraped by role page rather than by search query, like Wellfound: the pages are
a fixed taxonomy, and a query that does not map to one returns YC's marketing
page rather than an error.
"""

import logging

import httpx

from app.services.sources.base import LISTING_HEADERS, jobs_from_listing

logger = logging.getLogger(__name__)

_ROLE_URL = "https://www.ycombinator.com/jobs/role/{role}"

# YC's own role slugs. Not derived from the profile's target roles: a slug that
# does not exist serves a 200 marketing page, which would read as a board with
# no openings rather than as a bad request.
DEFAULT_ROLES = (
    "software-engineer",
    "backend-engineer",
    "frontend-engineer",
    "fullstack-engineer",
    "machine-learning-engineer",
    "data-engineer",
    "devops-engineer",
    "mobile-engineer",
)


def _roles() -> list[str]:
    from app.config import settings

    configured = getattr(settings, "YC_ROLES", "") or ""
    roles = [r.strip() for r in configured.split(",") if r.strip()]
    return roles or list(DEFAULT_ROLES)


def fetch(roles: list[str] | None = None) -> list[dict]:
    """
    Fetch YC jobs across the configured role pages.

    Called once per cycle rather than per query/location: the pages carry no
    location filter, so one pass covers every combination.
    """
    jobs: dict[str, dict] = {}
    for role in (roles if roles is not None else _roles()):
        try:
            found = jobs_from_listing(
                _ROLE_URL.format(role=role), "ycombinator", role, timeout=20
            )
        except httpx.HTTPError as exc:
            logger.warning("YC: role page '%s' failed: %s", role, exc)
            continue
        except Exception as exc:
            logger.error("YC: role page '%s' error: %s", role, exc)
            continue
        for job in found:
            # The same startup's posting appears under several role pages;
            # keeping the richest copy beats counting it repeatedly.
            existing = jobs.get(job["url"])
            if not existing or len(job["description"]) > len(existing["description"]):
                jobs[job["url"]] = job

    logger.info("YC: %d jobs across %d role pages", len(jobs), len(_roles()))
    return list(jobs.values())
