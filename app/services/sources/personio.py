"""
Personio job boards.

Personio publishes every customer's openings as an XML feed at
`<company>.jobs.personio.de/xml` — one request per company, full descriptions
included, no key and no pagination. That makes it the cheapest board in the
registry per job returned, and worth carrying a lot of slugs for.

The feed is German-hosted but not German-only: the `.de` is Personio's own
domain, and customers across Europe use it. Postings genuinely written in
German are handled downstream by the `language` column rather than by refusing
the source.
"""

import logging
import re
from xml.etree import ElementTree

import httpx

from app.services.descriptions import clean
from app.services.sources.base import (
    LISTING_HEADERS,
    board_workers,
    fetch_boards_concurrently,
    parse_experience_level,
)

logger = logging.getLogger(__name__)

_FEED = "https://{slug}.jobs.personio.de/xml"
_PUBLIC_URL = "https://{slug}.jobs.personio.de/job/{job_id}"

# Personio's own element names, and the ones customers' feeds vary between.
_ID_TAGS = ("id", "jobId")
_TITLE_TAGS = ("name", "jobName", "title", "subcompany")
_LOCATION_TAGS = ("office", "location", "city")
_DEPARTMENT_TAGS = ("department", "subcompany")
_DATE_TAGS = ("createdAt", "occupationCategory", "datePosted")


def _first(node, tags: tuple[str, ...]) -> str:
    for tag in tags:
        found = node.find(tag)
        if found is not None and (found.text or "").strip():
            return found.text.strip()
    return ""


def _description(node) -> str:
    """
    Personio splits a description into named sections, in order.

    Reading only the first would drop the requirements, which is the half the
    skill filter and the matcher actually care about.
    """
    parts: list[str] = []
    for block in node.iter("jobDescription"):
        name = (block.findtext("name") or "").strip()
        body = (block.findtext("value") or "").strip()
        if not body:
            continue
        parts.append(f"{name}\n{body}" if name else body)
    if not parts:
        # Some feeds put the whole thing in one flat element instead.
        for tag in ("description", "jobDescriptions"):
            text = (node.findtext(tag) or "").strip()
            if text:
                parts.append(text)
                break
    return clean("\n\n".join(parts))


def _positions(root):
    """Every posting element, whatever the feed wraps them in."""
    for tag in ("position", "job", "vacancy"):
        found = root.findall(f".//{tag}")
        if found:
            return found
    return []


def fetch(company_slugs: list[str]) -> list[dict]:
    """Fetch jobs from Personio's public XML feeds (no key required)."""

    def _fetch_one(slug: str) -> list[dict]:
        resp = httpx.get(
            _FEED.format(slug=slug), headers=LISTING_HEADERS,
            timeout=20, follow_redirects=True,
        )
        resp.raise_for_status()
        try:
            root = ElementTree.fromstring(resp.content)
        except ElementTree.ParseError as exc:
            raise ValueError(f"feed was not XML ({exc})") from exc
        # A short error page is often well-formed enough to parse, and would
        # then read as a feed with no openings — which is indistinguishable
        # from a company that is genuinely not hiring. The root tag tells them
        # apart.
        if root.tag.lower().endswith("html"):
            raise ValueError("feed was not XML (an HTML page came back instead)")

        jobs = []
        for node in _positions(root):
            title = _first(node, _TITLE_TAGS)
            job_id = _first(node, _ID_TAGS)
            if not title or not job_id:
                continue
            location = _first(node, _LOCATION_TAGS)
            description = _description(node)
            department = _first(node, _DEPARTMENT_TAGS)
            jobs.append({
                "source": "personio",
                "source_job_id": job_id,
                "title": title,
                "company": (root.findtext(".//company") or slug).strip() or slug,
                "location": location,
                "is_remote": bool(
                    re.search(r"remote|home ?office", f"{location} {title} {department}", re.I)
                ),
                "url": _PUBLIC_URL.format(slug=slug, job_id=job_id),
                "description": description,
                "experience_level": parse_experience_level(title, description),
                "posted_at": _first(node, _DATE_TAGS) or None,
            })
        return jobs

    return fetch_boards_concurrently(company_slugs, _fetch_one, "Personio", board_workers())
