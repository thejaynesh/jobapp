import logging

import httpx

from app.services.sources.base import (
    SourceUnavailable,
    parse_experience_level,
    raise_if_blocked,
)

logger = logging.getLogger(__name__)

_BASE = "https://jsearch.p.rapidapi.com/search"
_HOST = "jsearch.p.rapidapi.com"


def fetch(
    api_key: str,
    query: str,
    location: str,
    num_pages: int = 1,
) -> list[dict]:
    """
    Search JSearch (RapidAPI).

    The free tier is a few hundred calls a month, and this is called once per
    query/location pair — so a 403 (key rejected or quota spent) or 429 is
    raised as SourceUnavailable rather than swallowed. The caller abandons the
    source for the cycle instead of walking the remaining combinations and
    collecting an identical error for each one.
    """
    headers = {"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": _HOST}
    params = {"query": f"{query} in {location}", "num_pages": num_pages, "date_posted": "today"}
    try:
        resp = httpx.get(_BASE, headers=headers, params=params, timeout=15)
        raise_if_blocked(resp, "JSearch")
        resp.raise_for_status()
        data = resp.json()
    except SourceUnavailable:
        raise  # only "stop asking" propagates; the caller drops the source
    except Exception as exc:
        # Transient faults keep the existing contract: log and yield nothing, so
        # one bad query doesn't cost the rest. The log is surfaced by
        # services.source_diagnostics.
        logger.error("JSearch fetch error: %s", exc)
        return []

    jobs = []
    for item in data.get("data", []):
        title = item.get("job_title", "")
        desc = item.get("job_description", "")
        city = item.get("job_city", "")
        state = item.get("job_state", "")
        loc = ", ".join(filter(None, [city, state])) or item.get("job_country", "")
        jobs.append({
            "source": "jsearch",
            "source_job_id": item.get("job_id"),
            "title": title,
            "company": item.get("employer_name", ""),
            "location": loc,
            "is_remote": bool(item.get("job_is_remote", False)),
            "url": item.get("job_apply_link", ""),
            "description": desc,
            "experience_level": parse_experience_level(title, desc),
            "posted_at": item.get("job_posted_at_datetime_utc"),
        })
    return jobs
