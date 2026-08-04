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

# Wellfound role pages are a fixed taxonomy, not free text — deriving slugs
# from expanded search queries ("senior backend engineer python") would mostly
# request pages that don't exist. The roles to scrape are configured instead.
DEFAULT_ROLES = (
    "software-engineer",
    "full-stack-engineer",
    "backend-engineer",
    "mobile-engineer",
)

# A role page is the same regardless of which location asked for it.
_CACHE_TTL_SECONDS = 900
_cache: dict = {}


def role_slug(query: str) -> str:
    """'Senior Software Engineer' → 'senior-software-engineer'."""
    slug = re.sub(r"[^a-z0-9]+", "-", (query or "").lower()).strip("-")
    return slug or "software-engineer"


def configured_roles() -> list[str]:
    """Role slugs to scrape, from settings, falling back to the defaults."""
    from app.config import settings

    raw = getattr(settings, "WELLFOUND_ROLES", "") or ""
    slugs = [role_slug(s) for s in raw.split(",") if s.strip()]
    return slugs or list(DEFAULT_ROLES)


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


async def _scrape_role(page, slug: str, location: str) -> list[dict]:
    """One role page, reusing an already-open browser page."""
    url = _ROLE_URL.format(slug=slug)
    try:
        response = await page.goto(url, timeout=30000, wait_until="domcontentloaded")
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception as exc:
        logger.warning("Wellfound: page load failed for %s: %s", url, exc)
        return []

    # A misconfigured slug is worth naming precisely — it looks identical to a
    # role that simply has no openings otherwise.
    status = getattr(response, "status", None)
    if status == 404:
        logger.warning("Wellfound: no such role page %s (404) — check the slug", url)
        return []

    # A page with no text at all is a block, not a markup problem — there is
    # nothing on it to select, so say that rather than blaming selectors.
    try:
        await page.wait_for_function(
            "() => (document.body?.innerText || '').trim().length > 200",
            timeout=15000,
        )
    except Exception:
        # Include the HTTP status: 403 is a hard block, whereas 200-with-nothing
        # means the response was empty or the app refused to boot for us. Those
        # want different answers, and the page text alone can't tell them apart.
        logger.warning(
            "Wellfound: %s rendered no text at all (HTTP %s) — %s",
            url, status if status is not None else "unknown", await describe_page(page),
        )
        return []

    try:
        rows = await page.evaluate(_EXTRACT_JS)
    except Exception as exc:
        logger.warning("Wellfound: extraction failed (%s) — %s",
                       type(exc).__name__, await describe_page(page))
        return []

    jobs = _to_jobs(rows or [], location)

    if not jobs:
        # Last resort: the embedded app state, which sometimes carries the
        # listings even when neither structured data nor links do.
        raw = await page.evaluate("""() => [...document.querySelectorAll(
            'script[type="application/json"],script[id*="__NEXT_DATA__"]'
        )].map(s => s.textContent).join('|||')""")
        jobs = _parse_json_data(raw, location)

    if not jobs:
        logger.warning("Wellfound: no jobs found on %s — %s",
                       url, await describe_page(page))
    logger.info("Wellfound: %d jobs for role %s", len(jobs), slug)
    return jobs


_LD_JSON_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)
_JOB_ANCHOR_RE = re.compile(
    r'<a[^>]+href="(/jobs/\d+[^"]*)"[^>]*>(.*?)</a>', re.S | re.I
)
_TAG_RE = re.compile(r"<[^>]+>")

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _jobs_from_html(html: str, location: str) -> list[dict]:
    """Pull postings out of server-rendered HTML, no browser involved."""
    rows: list[dict] = []

    for blob in _LD_JSON_RE.findall(html or ""):
        try:
            data = json.loads(blob.strip())
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            for node in item.get("@graph", [item]):
                if not isinstance(node, dict) or node.get("@type") != "JobPosting":
                    continue
                org = node.get("hiringOrganization") or {}
                loc_node = node.get("jobLocation") or {}
                if isinstance(loc_node, list):
                    loc_node = loc_node[0] if loc_node else {}
                addr = (loc_node or {}).get("address") or {}
                rows.append({
                    "title": node.get("title") or "",
                    "company": org if isinstance(org, str) else (org.get("name") or ""),
                    "location": ", ".join(filter(None, [
                        addr.get("addressLocality"), addr.get("addressRegion"),
                    ])),
                    "url": node.get("url") or "",
                    "description": _TAG_RE.sub(" ", node.get("description") or "").strip(),
                    "remote": bool(node.get("jobLocationType")),
                })

    if not rows:
        seen = set()
        for href, inner in _JOB_ANCHOR_RE.findall(html or ""):
            title = _TAG_RE.sub(" ", inner).strip()
            title = " ".join(title.split())
            if not title or len(title) < 3 or title in seen:
                continue
            seen.add(title)
            rows.append({
                "title": title, "company": "", "location": "",
                "url": f"https://wellfound.com{href}",
                "description": "", "remote": False,
            })

    return _to_jobs(rows, location)


def _fetch_role_over_http(slug: str, location: str) -> list[dict]:
    """
    Try the role page with a plain HTTP client.

    Headless Chromium gets an empty response from Wellfound — zero characters
    of body and the bare hostname as the title, which is what Chromium shows
    when nothing came back. A plain client presents a completely different
    TLS/HTTP2 fingerprint, and these pages are server-rendered, so it's worth
    trying before paying for a browser at all.
    """
    import httpx

    url = _ROLE_URL.format(slug=slug)
    try:
        resp = httpx.get(url, headers=_HTTP_HEADERS, timeout=20, follow_redirects=True)
    except Exception as exc:
        logger.info("Wellfound: plain HTTP failed for %s (%s)", slug, exc)
        return []

    if resp.status_code != 200:
        logger.info("Wellfound: plain HTTP got %s for %s", resp.status_code, url)
        return []

    jobs = _jobs_from_html(resp.text, location)
    logger.info("Wellfound: plain HTTP returned %d bytes, %d jobs for %s",
                len(resp.text), len(jobs), slug)
    return jobs


async def fetch_roles(slugs: list[str] | None = None, location: str = "") -> list[dict]:
    """
    Fetch every configured role page.

    Plain HTTP first — it's free, and these pages are server-rendered. Only if
    that yields nothing does this fall back to a browser, and then a single
    session covers every remaining role.
    """
    from playwright.async_api import async_playwright

    slugs = slugs or configured_roles()
    jobs: list[dict] = [job for s in slugs if _cached(s) for job in _cached(s)]
    fresh = [s for s in slugs if not _cached(s)]

    still_needed = []
    for slug in fresh:
        found = _fetch_role_over_http(slug, location)
        if found:
            _cache[slug] = (time.monotonic(), found)
            jobs.extend(found)
        else:
            still_needed.append(slug)

    if not still_needed:
        logger.info("Wellfound: %d jobs over plain HTTP, no browser needed", len(jobs))
        return jobs
    fresh = still_needed

    async with async_playwright() as p:
        browser = await p.chromium.launch(**LAUNCH_OPTIONS)
        context = await browser.new_context(**CONTEXT_OPTIONS)
        page = await context.new_page()
        try:
            for slug in fresh:
                try:
                    found = await _scrape_role(page, slug, location)
                except Exception as exc:
                    logger.error("Wellfound: role %s failed: %s", slug, exc)
                    found = []
                _cache[slug] = (time.monotonic(), found)
                jobs.extend(found)
        finally:
            await browser.close()

    logger.info("Wellfound: %d jobs across %d role pages", len(jobs), len(slugs))
    return jobs


def _cached(slug: str) -> list[dict] | None:
    entry = _cache.get(slug)
    if entry and (time.monotonic() - entry[0]) < _CACHE_TTL_SECONDS:
        return entry[1]
    return None


def _parse_json_data(raw: str, location: str) -> list[dict]:
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


async def _scrape(query: str, location: str = "") -> list[dict]:
    """The single-role entry point, mirroring the other Playwright adapters."""
    return await fetch_roles([role_slug(query)], location)


async def fetch(query: str, location: str = "") -> list[dict]:
    """Jobs for the single role page matching `query`."""
    try:
        return await _scrape(query, location)
    except Exception as exc:
        logger.error("Wellfound scraper error: %s", exc)
        return []


def reset_cache() -> None:
    """Drop cached role pages (used by tests)."""
    _cache.clear()
