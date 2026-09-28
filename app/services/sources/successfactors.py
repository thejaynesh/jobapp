"""
SAP SuccessFactors career sites (Recruiting Marketing / Career Site Builder).

The ATS behind ~10–13% of large US employers — L3Harris, Qorvo and many more,
nearly always on the company's own domain (`jobs.l3harris.com`), which is why
no slug pattern ever found them.

Every such site serves an undocumented RSS feed at `/sitemal.xml` (the name
came from a typo of "sitemap", and stuck): every posting on the site, **with
its full description**, its employer and its location, in one request — 2,194
postings for L3Harris, 251 for Qorvo, measured 2026-09-28. So a whole site
costs one GET where a detail pass would cost thousands.

The feed carries no posting date (only an expiry), so `posted_at` is left
empty rather than guessed.

A board is the careers host itself: `careers.qorvo.com`.
"""

import html
import logging
import re
import xml.etree.ElementTree as ET

import httpx

from app.services.descriptions import clean as clean_description
from app.services.sources.base import (
    LISTING_HEADERS,
    board_workers,
    company_from_host,
    fetch_boards_concurrently,
    parse_experience_level,
    passing_titles,
)

logger = logging.getLogger(__name__)

FEED_PATH = "/sitemal.xml"
_G = "{http://base.google.com/ns/1.0}"
_TIMEOUT = 60
_HOST_RE = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)+$", re.I)
# "Principal RFIC Design Engineer (Chelmsford, MA, US, 1824)": the location is
# repeated in the title. "Chelmsford, MA, US, 1824": the last part is a code.
_TITLE_PLACE = re.compile(r"\s*\([^()]*\)\s*$")
_TRAILING_CODE = re.compile(r",\s*[A-Z0-9-]*\d[A-Z0-9-]*\s*$")


def feed_url(host: str) -> str:
    return f"https://{host}{FEED_PATH}"


def parse_feed(xml_text: str | bytes) -> list[dict]:
    """Every item in a sitemal feed as a plain dict. Raises on malformed XML."""
    root = ET.fromstring(xml_text)
    items = []
    for item in root.iter("item"):
        def text(tag: str) -> str:
            node = item.find(tag)
            return (node.text or "").strip() if node is not None else ""
        items.append({
            "title": text("title"),
            "link": text("link"),
            "id": text(f"{_G}id") or text("guid"),
            "employer": text(f"{_G}employer"),
            "location": text(f"{_G}location"),
            "function": text(f"{_G}job_function"),
            "description": text("description"),
        })
    return items


def _title(raw: str, location: str) -> str:
    """The title without the location Career Site Builder appends to it."""
    stripped = _TITLE_PLACE.sub("", raw).strip()
    return stripped or raw.strip()


def _location(raw: str) -> str:
    return _TRAILING_CODE.sub("", raw).strip()


def fetch(company_slugs: list[str], queries: list[str] | None = None) -> list[dict]:
    """
    Each site's whole feed, kept to the titles matching would accept.

    These are large employers' entire openings — two thousand at L3Harris, most
    of them in finance, HR and manufacturing — so the title gate is applied
    here rather than storing the rest for the matcher to reject.
    """
    queries = list(queries or [])

    def _fetch_one(host: str) -> list[dict]:
        host = (host or "").strip().lower()
        if not _HOST_RE.match(host):
            logger.warning("SuccessFactors: not a careers host: %r", host)
            return []
        resp = httpx.get(feed_url(host), headers=LISTING_HEADERS, timeout=_TIMEOUT,
                         follow_redirects=True)
        resp.raise_for_status()
        items = parse_feed(resp.content)
        kept = passing_titles(items, queries, lambda i: _title(i["title"], i["location"]))
        jobs = []
        for item in kept:
            title = _title(item["title"], item["location"])
            if not title or not item["link"]:
                continue
            location = _location(item["location"])
            description = clean_description(html.unescape(item["description"]))
            jobs.append({
                "source": "successfactors",
                "source_job_id": f"{host}:{item['id']}" if item["id"] else None,
                "title": title,
                "company": item["employer"] or company_from_host(host),
                "location": location,
                "is_remote": "remote" in f"{title} {location}".lower(),
                "url": item["link"],
                "description": description,
                "experience_level": parse_experience_level(title, description),
                "posted_at": None,
            })
        return jobs

    return fetch_boards_concurrently(company_slugs, _fetch_one, "SuccessFactors",
                                     board_workers())
