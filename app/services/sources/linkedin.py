import logging
import re

import httpx

from app.services.sources.base import parse_experience_level
from app.services.sources.playwright_base import encode, is_remote_location

logger = logging.getLogger(__name__)

_GUEST_API = (
    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    "?keywords={query}&location={location}&start=0"
)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def _strip(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html).strip()


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
    for i, job_url in enumerate(job_urls):
        title = titles[i] if i < len(titles) else ""
        company = companies[i] if i < len(companies) else ""
        loc = locations[i] if i < len(locations) else ""
        if not title:
            continue
        jobs.append({
            "source": "linkedin",
            "source_job_id": None,
            "title": title,
            "company": company,
            "location": loc,
            "is_remote": is_remote_location(loc, title),
            "url": job_url,
            "description": "",
            "experience_level": "mid",
        })
    logger.info("LinkedIn guest API: %d jobs for %s / %s", len(jobs), query, location)
    return jobs
