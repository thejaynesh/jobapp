"""
Jobspresso — curated remote jobs with an RSS feed.

The feed is free, carries full descriptions, and covers tech, marketing,
customer support, and management roles. Each posting is hand-screened by
Jobspresso staff, so the signal-to-noise is high — fewer jobs than an
aggregator, but almost all of them are real, current, and genuinely remote.
"""

import logging
import re
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

import httpx

from app.services.descriptions import clean as clean_description
from app.services.sources.base import parse_experience_level

logger = logging.getLogger(__name__)

_RSS = "https://jobspresso.co/?feed=job_feed"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml,application/xml;q=0.9,*/*;q=0.8",
}

_STRIP_RE = re.compile(r"<[^>]+>")
_GUID_RE = re.compile(r"[?&]p=(\d+)")


def _strip(html: str) -> str:
    return _STRIP_RE.sub("", html).strip()


def fetch(query: str) -> list[dict]:
    """Fetch remote jobs from Jobspresso's RSS feed, filtered by query."""
    try:
        resp = httpx.get(_RSS, headers=_HEADERS, timeout=20, follow_redirects=True)
        resp.raise_for_status()
    except Exception as exc:
        logger.error("Jobspresso fetch error: %s", exc)
        return []

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as exc:
        logger.error("Jobspresso RSS parse error: %s", exc)
        return []

    channel = root.find("channel")
    if channel is None:
        return []

    q_words = set(query.lower().split())
    jobs: list[dict] = []

    for item in channel.findall("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue

        desc_raw = item.findtext("description") or ""
        content = item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or ""
        desc = clean_description(content or desc_raw)
        company = (item.findtext("{https://jobspresso.co}company") or "").strip()
        location = (item.findtext("{https://jobspresso.co}location") or "").strip()

        if not company and " at " in title:
            parts = title.rsplit(" at ", 1)
            title, company = parts[0].strip(), parts[1].strip()
        elif not company and " - " in title:
            parts = title.rsplit(" - ", 1)
            title, company = parts[0].strip(), parts[1].strip()

        searchable = f"{title} {company} {desc[:200]}".lower()
        if q_words and not any(w in searchable for w in q_words):
            continue

        guid = (item.findtext("guid") or "").strip()
        id_match = _GUID_RE.search(guid)
        source_job_id = id_match.group(1) if id_match else None

        pub_date = (item.findtext("pubDate") or "").strip() or None
        try:
            pub_date = parsedate_to_datetime(pub_date).isoformat() if pub_date else None
        except (TypeError, ValueError, OverflowError):
            pub_date = None

        jobs.append({
            "source": "jobspresso",
            "source_job_id": source_job_id,
            "title": title,
            "company": company,
            "location": location or "Remote",
            "is_remote": True,
            "url": link,
            "description": desc,
            "experience_level": parse_experience_level(title, desc),
            "posted_at": pub_date,
        })

    logger.info("Jobspresso: %d jobs for query '%s'", len(jobs), query)
    return jobs
