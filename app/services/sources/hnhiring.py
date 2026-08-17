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

# Segments that name a place rather than an employer. The state-code pattern
# ('New York, NY') is the one that matters most: it is how almost every US
# location segment in the thread is written.
#
# The bare region names are anchored to the whole segment on purpose. Matched
# loosely they reject real employers — "UK Power Networks" is a company, and
# dropping it to avoid a location would trade one wrong answer for a missing
# job, which is the worse of the two.
_LOCATION_LIKE = re.compile(
    r",\s*[A-Z]{2}\b"                                   # New York, NY
    r"|\(\s*(in-?office|on-?site|onsite|hybrid|remote)"  # ... (In-Office)
    r"|^\s*(usa|u\.s\.a?\.?|uk|eu|emea|apac|latam|worldwide|anywhere"
    r"|global(ly)?|bay area|silicon valley|nyc|sf)\s*$",
    re.IGNORECASE,
)


def _parse_header(text: str) -> tuple[str, str]:
    """
    Posts follow the thread's own template: `Company | Role | Location | ...`.

    So the company is the *earliest* segment that isn't the role and isn't a
    place — position is the signal, and reading it as anything else is what
    stored "New York, NY (In-Office)" as an employer. The previous rule
    preferred comma-free segments over leading ones, which inverts exactly on
    the two common headers: a company written "Acme, Inc." loses to the
    location beside it, and a header of just `Location | Role` has no
    comma-free candidate at all and falls back to the location.

    The role is still found by content rather than position, because plenty of
    posters swap the first two segments.
    """
    first_line = text.split("\n", 1)[0].strip()
    parts = [p.strip() for p in first_line.split("|") if p.strip()]
    if len(parts) < 2:
        return (parts[0][:120] if parts else "", first_line[:150])

    title_idx = next(
        (i for i, p in enumerate(parts) if _CORE_ROLE.search(p)),
        next((i for i, p in enumerate(parts) if _ANY_ROLE.search(p)), None),
    )
    if title_idx is None:
        title_idx = next(
            (i for i, p in enumerate(parts[1:], start=1)
             if not _NON_COMPANY.search(p)),
            1,
        )

    company = next(
        (p for i, p in enumerate(parts)
         if i != title_idx
         and not _NON_COMPANY.search(p)
         and not _LOCATION_LIKE.search(p)),
        "",  # a header of only a place and a role names no employer
    )
    return company[:120], parts[title_idx][:150]


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
    skipped_no_company = 0

    for comment in story.get("children", []):
        raw = comment.get("text")
        if not raw:  # deleted/dead comments have no text
            continue
        text = _strip_html(raw)
        if q_words and not any(w in text.lower() for w in q_words):
            continue

        company, title = _parse_header(text)
        # No employer means no job worth storing: the company is half of the
        # dedupe key, and an empty one collapses unrelated posts into each
        # other. Rarer than it sounds — it needs a header that names only a
        # place and a role.
        if not title or not company:
            skipped_no_company += 1 if title else 0
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

    logger.info(
        "HN hiring: %d jobs from thread %s (%d posts named no company)",
        len(jobs), story_id, skipped_no_company,
    )
    return jobs
