import logging
import re
from xml.etree import ElementTree as ET

import httpx

from app.services.sources.base import parse_experience_level

logger = logging.getLogger(__name__)

_FEEDS = [
    "https://weworkremotely.com/categories/remote-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
    "https://weworkremotely.com/categories/remote-data-science-jobs.rss",
]
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml,application/xml;q=0.9,*/*;q=0.8",
}


def fetch(query: str) -> list[dict]:
    """Fetch remote tech jobs from We Work Remotely RSS feeds."""
    q_words = set(query.lower().split())
    jobs: list[dict] = []
    seen: set[str] = set()

    for feed_url in _FEEDS:
        try:
            resp = httpx.get(feed_url, headers=_HEADERS, timeout=15, follow_redirects=True)
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
        except Exception as exc:
            logger.warning("WWR RSS fetch error (%s): %s", feed_url, exc)
            continue

        channel = root.find("channel")
        if not channel:
            continue

        for item in channel.findall("item"):
            title_raw = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()

            if not link or link in seen:
                continue

            # WWR title format: "Company Name: Job Title"
            company = ""
            title = title_raw
            if ": " in title_raw:
                parts = title_raw.split(": ", 1)
                company = parts[0].strip()
                title = parts[1].strip()

            searchable = title.lower()
            if q_words and not q_words.intersection(searchable.split()):
                if not any(w in searchable for w in q_words):
                    continue

            seen.add(link)
            desc_raw = item.findtext("description") or ""
            desc = re.sub(r"<[^>]+>", "", desc_raw).strip()

            jobs.append({
                "source": "weworkremotely",
                "source_job_id": None,
                "title": title,
                "company": company,
                "location": "Remote",
                "is_remote": True,
                "url": link,
                "description": desc,
                "experience_level": parse_experience_level(title, desc),
                "posted_at": item.findtext("pubDate"),
            })

    logger.info("WeWorkRemotely: %d jobs for query '%s'", len(jobs), query)
    return jobs
