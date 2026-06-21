import logging

import httpx

from app.services.sources.base import parse_experience_level

logger = logging.getLogger(__name__)

_BASE = "https://remotive.com/api/remote-jobs"
_CATEGORIES = ["software-dev", "devops-sysadmin", "data", "product"]


def fetch(query: str) -> list[dict]:
    """Fetch remote tech jobs from Remotive's free public API."""
    jobs: list[dict] = []
    seen: set[str] = set()
    q_words = set(query.lower().split())

    for category in _CATEGORIES:
        try:
            resp = httpx.get(
                _BASE,
                params={"category": category, "limit": 50},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("Remotive fetch error (%s): %s", category, exc)
            continue

        for item in data.get("jobs", []):
            job_id = str(item.get("id", ""))
            if job_id in seen:
                continue

            title = (item.get("title") or "").strip()
            tags_text = " ".join(item.get("tags") or []).lower()
            searchable = (title + " " + tags_text).lower()

            # Keep only if any query word appears in title or tags
            if q_words and not q_words.intersection(searchable.split()):
                if not any(w in searchable for w in q_words):
                    continue

            seen.add(job_id)
            desc = item.get("description") or ""
            jobs.append({
                "source": "remotive",
                "source_job_id": job_id,
                "title": title,
                "company": (item.get("company_name") or "").strip(),
                "location": (item.get("candidate_required_location") or "Remote").strip(),
                "is_remote": True,
                "url": item.get("url") or "",
                "description": desc,
                "experience_level": parse_experience_level(title, desc),
                "posted_at": item.get("publication_date"),
            })

    logger.info("Remotive: %d jobs for query '%s'", len(jobs), query)
    return jobs
