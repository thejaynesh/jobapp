"""
USAJOBS — the US federal government's official jobs API.

Free, keyed, documented and stable, which makes it the most reliable source in
the whole set. It is also the only one that states pay on every single posting,
because federal salary ranges are public by law — so those go straight into the
salary columns instead of being re-derived from prose by a model call.

Registration is two fields rather than one: the API identifies callers by the
email address they registered with as well as by the key, and sends 401 for a
request carrying only one of them.
"""

import logging

import httpx

from app.services.descriptions import clean
from app.services.sources.base import (
    SourceUnavailable,
    parse_experience_level,
    raise_if_blocked,
)

logger = logging.getLogger(__name__)

_BASE = "https://data.usajobs.gov/api/search"
_HOST = "data.usajobs.gov"

# The API caps a page at 500; asking for more is an error rather than a cap.
_RESULTS_PER_PAGE = 250

# Pay is quoted per year, per hour, or as a one-off. Only the annual figures
# belong in a column the salary filter compares against a yearly floor.
_ANNUAL_INTERVALS = {"PA", "PER YEAR", "ANNUAL"}

_SCHEDULE_TO_TYPE = {
    "full-time": "full_time",
    "part-time": "part_time",
    "intermittent": "part_time",
    "internship": "internship",
    "temporary": "contract",
    "seasonal": "contract",
}


def _text(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        for item in value:
            found = _text(item)
            if found:
                return found
    if isinstance(value, dict):
        for key in ("Name", "name", "Value", "DisplayName"):
            if key in value:
                return _text(value[key])
    return ""


def _salary(descriptor: dict) -> dict:
    """
    The stated pay range, when it is quoted per year.

    Hourly grades are left null rather than annualised: the posting did not
    state a yearly figure, and inventing one would put a number nobody wrote
    down in front of the salary filter.
    """
    for entry in descriptor.get("PositionRemuneration") or []:
        if not isinstance(entry, dict):
            continue
        interval = _text(entry.get("RateIntervalCode") or entry.get("Description")).upper()
        if interval and interval not in _ANNUAL_INTERVALS:
            continue
        try:
            low = float(entry.get("MinimumRange"))
            high = float(entry.get("MaximumRange"))
        except (TypeError, ValueError):
            continue
        if low <= 0 and high <= 0:
            continue
        if high < low:
            low, high = high, low
        return {
            "salary_min": low or high,
            "salary_max": high or low,
            "salary_currency": "USD",
        }
    return {}


def _employment_type(descriptor: dict) -> str | None:
    schedule = _text(descriptor.get("PositionSchedule")).lower()
    for key, value in _SCHEDULE_TO_TYPE.items():
        if key in schedule:
            return value
    return None


def _description(descriptor: dict) -> str:
    """
    The summary plus the duties, which are separate fields.

    The summary alone is a paragraph of framing; the duties are what the skill
    filter and the matcher are actually reading for.
    """
    details = (descriptor.get("UserArea") or {}).get("Details") or {}
    parts = [
        descriptor.get("QualificationSummary") or "",
        details.get("JobSummary") or "",
        details.get("MajorDuties") or "",
        details.get("Requirements") or "",
    ]
    flattened = []
    for part in parts:
        if isinstance(part, list):
            flattened.extend(str(p) for p in part)
        elif part:
            flattened.append(str(part))
    return clean("\n\n".join(flattened))


def fetch(api_key: str, user_agent: str, query: str, location: str = "",
          max_pages: int = 2) -> list[dict]:
    """Search USAJOBS for one query/location pair."""
    headers = {
        "Host": _HOST,
        "User-Agent": user_agent,
        "Authorization-Key": api_key,
    }

    jobs: list[dict] = []
    for page in range(1, max(1, max_pages) + 1):
        params = {
            "Keyword": query,
            "ResultsPerPage": _RESULTS_PER_PAGE,
            "Page": page,
            "SortField": "opendate",
            "SortDirection": "desc",
        }
        if location:
            params["LocationName"] = location

        try:
            resp = httpx.get(_BASE, headers=headers, params=params, timeout=20)
            # A rejected key answers every remaining query identically, so the
            # caller drops the source for the cycle rather than asking 40 times.
            raise_if_blocked(resp, "USAJOBS")
            resp.raise_for_status()
            data = resp.json()
        except SourceUnavailable:
            raise
        except Exception as exc:
            logger.error("USAJOBS fetch error (%s / %s): %s", query, location, exc)
            break

        items = (data.get("SearchResult") or {}).get("SearchResultItems") or []
        for item in items:
            descriptor = (item or {}).get("MatchedObjectDescriptor") or {}
            title = _text(descriptor.get("PositionTitle"))
            url = _text(descriptor.get("PositionURI"))
            if not title or not url:
                continue
            description = _description(descriptor)
            location_text = _text(descriptor.get("PositionLocationDisplay"))
            jobs.append({
                "source": "usajobs",
                "source_job_id": _text(item.get("MatchedObjectId"))
                or _text(descriptor.get("PositionID")),
                "title": title,
                "company": _text(descriptor.get("OrganizationName"))
                or _text(descriptor.get("DepartmentName")),
                "location": location_text,
                "is_remote": "remote" in f"{location_text} {title}".lower(),
                "url": url,
                "apply_url": _text(descriptor.get("ApplyURI")) or None,
                "description": description,
                "experience_level": parse_experience_level(title, description),
                "posted_at": descriptor.get("PublicationStartDate"),
                "employment_type": _employment_type(descriptor),
                **_salary(descriptor),
            })

        if len(items) < _RESULTS_PER_PAGE:
            break  # the result set is exhausted

    logger.info("USAJOBS: %d jobs for '%s' / '%s'", len(jobs), query, location or "any")
    return jobs
