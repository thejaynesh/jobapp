import logging
import re
from xml.etree import ElementTree as ET

import httpx

from app.services.sources.base import parse_experience_level
from app.services.sources.playwright_base import encode, is_remote_location

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) "
        "Gecko/20100101 Firefox/120.0"
    ),
    "Accept": "application/rss+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _strip(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html).strip()


def fetch(query: str, location: str) -> list[dict]:
    """Fetch Indeed jobs via RSS feed — no browser required."""
    url = (
        f"https://www.indeed.com/rss"
        f"?q={encode(query)}&l={encode(location)}&fromage=7&sort=date"
    )
    try:
        resp = httpx.get(url, headers=_HEADERS, timeout=15, follow_redirects=True)
        resp.raise_for_status()
    except Exception as exc:
        logger.error("Indeed RSS fetch error: %s", exc)
        return []

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as exc:
        logger.error("Indeed RSS parse error: %s", exc)
        return []

    channel = root.find("channel")
    if not channel:
        return []

    jobs = []
    for item in channel.findall("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc_raw = (item.findtext("description") or "").strip()
        desc = _strip(desc_raw)
        # Company is sometimes in <source> or embedded in title "Role - Company"
        company = (item.findtext("source") or "").strip()
        if not company and " - " in title:
            parts = title.rsplit(" - ", 1)
            title, company = parts[0].strip(), parts[1].strip()

        if not title or not link:
            continue

        jobs.append({
            "source": "indeed",
            "source_job_id": None,
            "title": title,
            "company": company,
            "location": location,
            "is_remote": is_remote_location(location, title),
            "url": link,
            "description": desc,
            "experience_level": parse_experience_level(title, desc),
        })
    logger.info("Indeed RSS: %d jobs for %s / %s", len(jobs), query, location)
    return jobs
