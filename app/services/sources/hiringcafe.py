"""
hiring.cafe — an aggregator that indexes ATS boards directly.

Worth having because of what it aggregates: it crawls Greenhouse, Lever, Ashby
and friends rather than other job boards, so its postings carry full
descriptions and link at the employer rather than at a redirect page. That is
the opposite of the Adzuna/Jooble problem the whole of Phase 1 was spent on.

Its search endpoint is undocumented and its response shape is not ours to rely
on, so this does not read a fixed path through the JSON. It reuses the
shape-based reader written for the browser harvest: walk the whole payload and
take any object that looks like a job — a title, a company, and something to
identify it by. A redesign that moves the nesting keeps working, and a rename
of every field at once shows up as a sudden drop to zero rather than as quiet
corruption.
"""

import logging

import httpx

from app.services.descriptions import clean
from app.services.harvest import extract_jobs
from app.services.sources.base import (
    LISTING_HEADERS,
    SourceUnavailable,
    parse_experience_level,
    raise_if_blocked,
)

logger = logging.getLogger(__name__)

_SEARCH = "https://hiring.cafe/api/search-jobs"

# Enough to be worth the request, small enough that one bad query cannot own
# the cycle.
_PAGE_SIZE = 100


def fetch(query: str, location: str = "") -> list[dict]:
    """Search hiring.cafe for one query/location pair."""
    payload = {
        "searchQuery": query,
        "size": _PAGE_SIZE,
        "page": 0,
    }
    if location:
        payload["locationQuery"] = location

    try:
        resp = httpx.post(
            _SEARCH, json=payload, headers=LISTING_HEADERS,
            timeout=20, follow_redirects=True,
        )
        raise_if_blocked(resp, "hiring.cafe")
        resp.raise_for_status()
        data = resp.json()
    except SourceUnavailable:
        raise
    except Exception as exc:
        logger.error("hiring.cafe fetch error (%s / %s): %s", query, location, exc)
        return []

    found = extract_jobs(data, source="hiringcafe")
    if not found and data:
        # A 200 that yields nothing job-shaped means the payload moved, which
        # is a different problem from "no results" and has to say so.
        logger.warning(
            "hiring.cafe: a response came back with no job-shaped objects in "
            "it (the payload may have changed shape)"
        )

    jobs = []
    for job in found:
        description = clean(job.get("description") or "")
        title = job["title"]
        jobs.append({
            **job,
            "description": description,
            "experience_level": parse_experience_level(title, description),
        })

    logger.info("hiring.cafe: %d jobs for '%s' / '%s'", len(jobs), query,
                location or "any")
    return jobs
