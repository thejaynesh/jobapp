import logging
import re
import time

import httpx

from app.services.sources.base import parse_experience_level
from app.services.sources.playwright_base import encode, is_remote_location

logger = logging.getLogger(__name__)

_GUEST_API = (
    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    "?keywords={query}&location={location}&start=0"
)
_POSTING_API = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Full-description fetches are one request per job; cap per search to stay polite.
_MAX_DETAIL_FETCHES = 10
_DETAIL_PAUSE_SECONDS = 0.5


def _strip(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html).strip()


def _job_id_from_url(url: str) -> str | None:
    """Job view URLs end in a numeric posting id: .../jobs/view/some-title-4012345678"""
    m = re.search(r"(\d{8,})/?$", url)
    return m.group(1) if m else None


def _fetch_description(job_id: str, headers: dict) -> str:
    """Fetch the full JD from the guest job-posting endpoint (plain text)."""
    try:
        resp = httpx.get(
            _POSTING_API.format(job_id=job_id),
            headers=headers, timeout=15, follow_redirects=True,
        )
        resp.raise_for_status()
        html = resp.text
    except Exception as exc:
        logger.warning("LinkedIn posting fetch error (%s): %s", job_id, exc)
        return ""

    m = re.search(
        r'class="show-more-less-html__markup[^"]*"[^>]*>(.*?)</div>',
        html, re.DOTALL,
    )
    if not m:
        return ""
    # Keep paragraph/bullet boundaries as newlines so the text stays readable.
    text = re.sub(r"<(?:br|/p|/li|/ul)[^>]*>", "\n", m.group(1))
    return re.sub(r"\n{3,}", "\n\n", _strip(text))


def fetch(session_cookie: str, query: str, location: str) -> list[dict]:
    """Fetch via LinkedIn guest jobs API — no browser required."""
    url = _GUEST_API.format(query=encode(query), location=encode(location))
    headers = dict(_HEADERS)
    if session_cookie:
        headers["Cookie"] = f"li_at={session_cookie}"
    try:
        resp = httpx.get(url, headers=headers, timeout=15, follow_redirects=True)
        resp.raise_for_status()
        html = resp.text
    except Exception as exc:
        logger.error("LinkedIn guest API error: %s", exc)
        return []

    # Parse stable class-name patterns from the HTML fragment response
    job_urls = re.findall(
        r'href="(https://www\.linkedin\.com/jobs/view/[^"?]+)', html
    )
    titles = [
        _strip(m)
        for m in re.findall(
            r'class="base-search-card__title[^"]*"[^>]*>(.*?)</h3>', html, re.DOTALL
        )
    ]
    companies = [
        _strip(m)
        for m in re.findall(
            r'class="base-search-card__subtitle[^"]*"[^>]*>.*?<[^>]+>(.*?)</',
            html, re.DOTALL,
        )
    ]
    locations = [
        _strip(m)
        for m in re.findall(
            r'class="job-search-card__location[^"]*"[^>]*>(.*?)</span>',
            html, re.DOTALL,
        )
    ]

    jobs = []
    detail_fetches = 0
    for i, job_url in enumerate(job_urls):
        title = titles[i] if i < len(titles) else ""
        company = companies[i] if i < len(companies) else ""
        loc = locations[i] if i < len(locations) else ""
        if not title:
            continue

        # Without a description the downstream skill filter rejects every job,
        # so fetch the full JD for the first N postings of each search.
        job_id = _job_id_from_url(job_url)
        description = ""
        if job_id and detail_fetches < _MAX_DETAIL_FETCHES:
            if detail_fetches:
                time.sleep(_DETAIL_PAUSE_SECONDS)
            description = _fetch_description(job_id, headers)
            detail_fetches += 1

        jobs.append({
            "source": "linkedin",
            "source_job_id": job_id,
            "title": title,
            "company": company,
            "location": loc,
            "is_remote": is_remote_location(loc, title),
            "url": job_url,
            "description": description,
            "experience_level": parse_experience_level(title, description),
        })
    logger.info("LinkedIn guest API: %d jobs for %s / %s", len(jobs), query, location)
    return jobs
