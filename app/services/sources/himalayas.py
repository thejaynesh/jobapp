import logging

import httpx

from app.services.sources.base import parse_experience_level

logger = logging.getLogger(__name__)

_BASE = "https://himalayas.app/jobs/api"

# The company arrives as a plain string on some records and as an object on
# others (`{"name": "Doist", "logo": ...}`). Reading one shape and trusting it
# is what put the literal string "name" in the company column of every stored
# Himalayas job: the key got read where the value was meant.
_COMPANY_KEYS = ("companyName", "company", "companyTitle", "employer")
_DESCRIPTION_KEYS = ("description", "excerpt", "summary")


def _text(value) -> str:
    """A display string out of whatever shape the field arrived in."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in ("name", "title", "label", "companyName"):
            if key in value:
                return _text(value[key])
        return ""
    if isinstance(value, list):
        for item in value:
            found = _text(item)
            if found:
                return found
    return ""


# Values that are a JSON key rather than a company. Every stored Himalayas job
# had one of these as its employer, so however the flattening happened, the
# result is refused here rather than written to the column again.
_KEY_NAMES = frozenset({"name", "title", "label", "company", "companyname", "value"})


def _first(item: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        found = _text(item.get(key))
        if found:
            return found
    return ""


def _company(item: dict) -> str:
    found = _first(item, _COMPANY_KEYS)
    return "" if found.lower() in _KEY_NAMES else found


def _location(item: dict) -> str:
    """Join the location restrictions, tolerating a string or an object."""
    raw = item.get("locationRestrictions")
    if isinstance(raw, str):
        return raw.strip() or "Remote"
    if isinstance(raw, dict):
        raw = list(raw.values())
    if isinstance(raw, list):
        parts = [p for p in (_text(entry) for entry in raw) if p]
        if parts:
            return ", ".join(parts)
    return "Remote"


def fetch(query: str) -> list[dict]:
    """Fetch remote tech jobs from Himalayas' free public API (no key required)."""
    try:
        resp = httpx.get(_BASE, params={"limit": 100}, timeout=15, follow_redirects=True)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.error("Himalayas fetch error: %s", exc)
        return []

    q_words = set(query.lower().split())
    jobs: list[dict] = []
    seen: set[str] = set()

    raw_jobs = data.get("jobs")
    if isinstance(raw_jobs, dict):  # keyed by id rather than listed
        raw_jobs = list(raw_jobs.values())
    if not isinstance(raw_jobs, list):
        raw_jobs = []

    for item in raw_jobs:
        if not isinstance(item, dict):
            continue
        title = _text(item.get("title"))
        categories = " ".join(
            c for c in (_text(x) for x in (item.get("categories") or [])) if c
        ).lower()
        searchable = (title + " " + categories).lower()
        if q_words and not any(w in searchable for w in q_words):
            continue

        url = _text(item.get("applicationLink")) or _text(item.get("guid"))
        job_id = _text(item.get("guid")) or url
        if not job_id or job_id in seen:
            continue
        seen.add(job_id)

        desc = _first(item, _DESCRIPTION_KEYS)
        location = _location(item)

        # pubDate is a unix timestamp (sometimes as a string); normalize to int
        posted_at = item.get("pubDate")
        try:
            posted_at = int(posted_at)
        except (TypeError, ValueError):
            posted_at = None

        jobs.append({
            "source": "himalayas",
            "source_job_id": job_id,
            "title": title,
            "company": _company(item),
            "location": location,
            "is_remote": True,
            "url": url,
            "description": desc,
            "experience_level": parse_experience_level(title, desc),
            "posted_at": posted_at,
        })

    logger.info("Himalayas: %d jobs for query '%s'", len(jobs), query)
    return jobs
