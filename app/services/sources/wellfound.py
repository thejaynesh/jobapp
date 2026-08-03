"""
Wellfound (formerly AngelList Talent).

The `/jobs?q=…&l=…` search route is a client-rendered app that, from a server,
returned a page with literally zero characters of text — no markup to fix, no
selector to correct. The `/role/<slug>` landing pages are the SEO surface for
the same listings and come back server-rendered, so that's what this scrapes.

Location isn't part of a role URL, so the same page would otherwise be loaded
once per configured location; results are cached per role for the cycle instead.
"""

import json
import logging
import re
import time

from app.services.sources.base import parse_experience_level
from app.services.sources.playwright_base import (
    CONTEXT_OPTIONS,
    LAUNCH_OPTIONS,
    describe_page,
    is_remote_location,
)

logger = logging.getLogger(__name__)

_ROLE_URL = "https://wellfound.com/role/{slug}"

# A role page is the same regardless of which location asked for it.
_CACHE_TTL_SECONDS = 900
_cache: dict = {}


def role_slug(query: str) -> str:
    """'Senior Software Engineer' → 'senior-software-engineer'."""
    slug = re.sub(r"[^a-z0-9]+", "-", (query or "").lower()).strip("-")
    return slug or "software-engineer"


# Layered like the Dice extractor: structured data first, then anchors, so a
# redesign of the cards doesn't take the whole source down.
_EXTRACT_JS = """() => {
    const out = [];
    const seen = new Set();
    const push = (job) => {
        if (!job || !job.title || !job.url || seen.has(job.url)) return;
        seen.add(job.url);
        out.push(job);
    };
    const abs = (href) => {
        if (!href) return '';
        if (href.startsWith('http')) return href;
        return 'https://wellfound.com' + (href.startsWith('/') ? href : '/' + href);
    };

    // 1. JSON-LD JobPosting — what the SEO pages exist to publish.
    for (const el of document.querySelectorAll('script[type="application/ld+json"]')) {
        let data;
        try { data = JSON.parse(el.textContent); } catch (e) { continue; }
        const items = Array.isArray(data) ? data : [data];
        for (const item of items) {
            const graph = item['@graph'] || [item];
            for (const node of graph) {
                if (!node || node['@type'] !== 'JobPosting') continue;
                const org = node.hiringOrganization || {};
                const locNode = Array.isArray(node.jobLocation)
                    ? (node.jobLocation[0] || {}) : (node.jobLocation || {});
                const addr = locNode.address || {};
                push({
                    title: (node.title || '').trim(),
                    company: (typeof org === 'string' ? org : org.name || '').trim(),
                    location: [addr.addressLocality, addr.addressRegion,
                               addr.addressCountry].filter(Boolean).join(', '),
                    url: abs(node.url || ''),
                    description: (node.description || '').replace(/<[^>]+>/g, ' ').trim(),
                    remote: !!node.jobLocationType,
                });
            }
        }
    }
    if (out.length) return out;

    // 2. Links to individual postings, whatever wraps them.
    const seenTitles = new Set();
    for (const a of document.querySelectorAll('a[href*="/jobs/"]')) {
        const href = a.getAttribute('href') || '';
        if (!/\\/jobs\\/\\d/.test(href)) continue;
        const title = (a.innerText || '').trim().split('\\n')[0];
        if (!title || title.length < 3 || seenTitles.has(title)) continue;
        seenTitles.add(title);
        let card = a;
        for (let i = 0; i < 4 && card.parentElement; i++) card = card.parentElement;
        const lines = (card.innerText || '').split('\\n')
            .map(s => s.trim()).filter(Boolean);
        const idx = lines.indexOf(title);
        push({
            title: title,
            company: idx > 0 ? lines[0] : (lines[idx + 1] || ''),
            location: idx >= 0 && lines[idx + 1] ? lines[idx + 1] : '',
            url: abs(href),
            description: '',
            remote: /remote/i.test(card.innerText || ''),
        });
    }
    return out;
}"""


def _to_jobs(rows: list[dict], fallback_location: str) -> list[dict]:
    jobs = []
    for row in rows:
        title = (row.get("title") or "").strip()
        url = (row.get("url") or "").strip()
        if not title or not url:
            continue
        loc = (row.get("location") or "").strip() or fallback_location
        desc = (row.get("description") or "").strip()
        jobs.append({
            "source": "wellfound",
            "source_job_id": None,
            "title": title,
            "company": (row.get("company") or "").strip(),
            "location": loc,
            "is_remote": bool(row.get("remote")) or is_remote_location(loc, title),
            "url": url,
            "description": desc,
            "experience_level": parse_experience_level(title, desc),
        })
    return jobs


async def _scrape(query: str, location: str) -> list[dict]:
    from playwright.async_api import async_playwright

    slug = role_slug(query)
    url = _ROLE_URL.format(slug=slug)

    async with async_playwright() as p:
        browser = await p.chromium.launch(**LAUNCH_OPTIONS)
        context = await browser.new_context(**CONTEXT_OPTIONS)
        page = await context.new_page()
        try:
            await page.goto(url, timeout=30000, wait_until="domcontentloaded")
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception as exc:
            logger.warning("Wellfound: page load failed for %s: %s", url, exc)
            await browser.close()
            return []

        # A page with no text at all is a block, not a markup problem — there is
        # nothing on it to select, so say that rather than blaming selectors.
        try:
            await page.wait_for_function(
                "() => (document.body?.innerText || '').trim().length > 200",
                timeout=15000,
            )
        except Exception:
            logger.warning("Wellfound: %s rendered no text at all — %s",
                           url, await describe_page(page))
            await browser.close()
            return []

        try:
            rows = await page.evaluate(_EXTRACT_JS)
        except Exception as exc:
            logger.warning("Wellfound: extraction failed (%s) — %s",
                           type(exc).__name__, await describe_page(page))
            await browser.close()
            return []

        jobs = _to_jobs(rows or [], location)

        if not jobs:
            # Last resort: the embedded app state, which sometimes carries the
            # listings even when neither structured data nor links do.
            raw = await page.evaluate("""() => [...document.querySelectorAll(
                'script[type="application/json"],script[id*="__NEXT_DATA__"]'
            )].map(s => s.textContent).join('|||')""")
            jobs = _parse_json_data(raw, query, location)

        if not jobs:
            logger.warning("Wellfound: no jobs found on %s — %s",
                           url, await describe_page(page))
        await browser.close()
        logger.info("Wellfound: %d jobs for %s", len(jobs), slug)
        return jobs


def _parse_json_data(raw: str, query: str, location: str) -> list[dict]:
    """Try to extract jobs from embedded page JSON blobs."""
    jobs: list[dict] = []
    for chunk in (raw or "").split("|||"):
        chunk = chunk.strip()
        if not chunk or len(chunk) < 50:
            continue
        try:
            _walk_json(json.loads(chunk), jobs)
        except Exception:
            pass
    return jobs


def _walk_json(node, jobs: list, depth: int = 0) -> None:
    if depth > 10 or not isinstance(node, (dict, list)):
        return
    if isinstance(node, list):
        for item in node:
            _walk_json(item, jobs, depth + 1)
        return
    title = node.get("title") or node.get("name") or ""
    url = node.get("url") or node.get("jobUrl") or node.get("applyUrl") or ""
    company = (
        node.get("startupName") or node.get("company") or
        (node.get("startup") or {}).get("name") or ""
    )
    loc = node.get("remote") or node.get("locationStr") or node.get("location") or ""
    if title and url and "wellfound.com" in str(url):
        jobs.append({
            "source": "wellfound",
            "source_job_id": str(node.get("id", "")),
            "title": title,
            "company": company if isinstance(company, str) else "",
            "location": loc if isinstance(loc, str) else "",
            "is_remote": "remote" in str(loc).lower() or bool(node.get("remote")),
            "url": url,
            "description": node.get("description") or "",
            "experience_level": parse_experience_level(title, ""),
        })
        return
    for v in node.values():
        _walk_json(v, jobs, depth + 1)


async def fetch(query: str, location: str) -> list[dict]:
    """
    Jobs for one role. Cached per role slug: the URL ignores location, so
    without this the same page would be loaded once per configured location.
    """
    slug = role_slug(query)
    cached = _cache.get(slug)
    if cached and (time.monotonic() - cached[0]) < _CACHE_TTL_SECONDS:
        return [dict(job, location=job["location"] or location) for job in cached[1]]

    try:
        jobs = await _scrape(query, location)
    except Exception as exc:
        logger.error("Wellfound scraper error: %s", exc)
        return []

    _cache[slug] = (time.monotonic(), jobs)
    return jobs


def reset_cache() -> None:
    """Drop cached role pages (used by tests)."""
    _cache.clear()
