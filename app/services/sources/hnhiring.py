import html as html_lib
import logging
import re

import httpx

from app.services.sources.base import parse_experience_level

logger = logging.getLogger(__name__)

_SEARCH_API = "https://hn.algolia.com/api/v1/search_by_date"
_ITEM_API = "https://hn.algolia.com/api/v1/items/{item_id}"
_THREAD_URL = "https://news.ycombinator.com/item?id={item_id}"


def _strip_html(text: str) -> str:
    text = re.sub(r"<p>", "\n\n", text or "")
    text = re.sub(r"<[^>]+>", "", text)
    return html_lib.unescape(text).strip()


def _latest_hiring_story_id() -> str | None:
    """Find the most recent monthly 'Ask HN: Who is hiring?' thread."""
    resp = httpx.get(
        _SEARCH_API,
        params={
            "query": "Ask HN: Who is hiring?",
            "tags": "story,author_whoishiring",
            "hitsPerPage": 5,
        },
        timeout=15,
    )
    resp.raise_for_status()
    for hit in resp.json().get("hits", []):
        if "who is hiring" in (hit.get("title") or "").lower():
            return str(hit.get("objectID"))
    return None


# Job-title words, strongest signals first: 'Founding Systems Engineer' should win
# over 'Solo Founder' when both appear in one header.
_CORE_ROLE = re.compile(
    r"\b(engineer|developer|scientist|architect|designer|analyst|intern"
    r"|sre|devops|programmer|swe)\b",
    re.IGNORECASE,
)
_ANY_ROLE = re.compile(r"\b(manager|founder|lead|recruiter|cto|head)\b", re.IGNORECASE)
_NON_COMPANY = re.compile(
    r"\b(remote|onsite|on-?site|hybrid|full-?time|part-?time|contract|visa|salary"
    r"|equity|benefits)\b|\$|€|£|\b\d{2,3}k\b",
    re.IGNORECASE,
)


def _parse_header(text: str) -> tuple[str, str]:
    """
    Posts conventionally start with pipe-separated segments, but their order
    varies ('Company | Role | Location', 'Location | Company | Role', ...).
    Pick the title as the segment containing a role word, and the company as
    the most company-looking remaining segment. Falls back to the first line
    when unpiped.
    """
    first_line = text.split("\n", 1)[0].strip()
    parts = [p.strip() for p in first_line.split("|") if p.strip()]
    if len(parts) >= 2:
        title = (
            next((p for p in parts if _CORE_ROLE.search(p)), None)
            or next((p for p in parts if _ANY_ROLE.search(p)), None)
            or next((p for p in parts[1:] if not _NON_COMPANY.search(p)), parts[1])
        )
        others = [p for p in parts if p != title and not _NON_COMPANY.search(p)]
        # Segments with commas are usually locations ('Blaine, WA'), not companies.
        company_candidates = [p for p in others if "," not in p] or others
        company = company_candidates[0] if company_candidates else parts[0]
        return company[:120], title[:150]
    return parts[0][:120] if parts else "", first_line[:150]


def fetch(queries: list[str]) -> list[dict]:
    """
    Fetch job posts from the latest monthly HN 'Who is hiring?' thread.
    Each top-level comment is one posting; keep those matching any query word.
    """
    try:
        story_id = _latest_hiring_story_id()
        if not story_id:
            logger.warning("HN hiring: no monthly thread found")
            return []
        resp = httpx.get(_ITEM_API.format(item_id=story_id), timeout=30)
        resp.raise_for_status()
        story = resp.json()
    except Exception as exc:
        logger.error("HN hiring fetch error: %s", exc)
        return []

    q_words = {w for q in queries for w in q.lower().split()}
    jobs: list[dict] = []

    for comment in story.get("children", []):
        raw = comment.get("text")
        if not raw:  # deleted/dead comments have no text
            continue
        text = _strip_html(raw)
        if q_words and not any(w in text.lower() for w in q_words):
            continue

        company, title = _parse_header(text)
        if not title:
            continue
        comment_id = str(comment.get("id", ""))

        text_lower = text.lower()
        is_remote = "remote" in text_lower
        location = "Remote" if is_remote else ""

        jobs.append({
            "source": "hnhiring",
            "source_job_id": comment_id,
            "title": title,
            "company": company,
            "location": location,
            "is_remote": is_remote,
            "url": _THREAD_URL.format(item_id=comment_id),
            "description": text,
            "experience_level": parse_experience_level(title, text),
            "posted_at": comment.get("created_at"),
        })

    logger.info("HN hiring: %d jobs from thread %s", len(jobs), story_id)
    return jobs
