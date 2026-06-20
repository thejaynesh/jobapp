import json
import logging

from app.services.sources.base import parse_experience_level
from app.services.sources.playwright_base import (
    CONTEXT_OPTIONS,
    LAUNCH_OPTIONS,
    encode,
    is_remote_location,
)

logger = logging.getLogger(__name__)


async def _scrape(query: str, location: str) -> list[dict]:
    from playwright.async_api import async_playwright

    url = f"https://wellfound.com/jobs?q={encode(query)}&l={encode(location)}"
    async with async_playwright() as p:
        browser = await p.chromium.launch(**LAUNCH_OPTIONS)
        context = await browser.new_context(**CONTEXT_OPTIONS)
        page = await context.new_page()
        try:
            await page.goto(url, timeout=30000, wait_until="domcontentloaded")
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception as exc:
            logger.warning("Wellfound: page load failed: %s", exc)
            await browser.close()
            return []

        # Try to extract data from embedded JSON first (most reliable)
        raw = await page.evaluate("""() => {
            const scripts = [...document.querySelectorAll('script[type="application/json"],' +
                                                          'script[id*="__NEXT_DATA__"],' +
                                                          'script[id="__RELAY_STORE__"]')];
            return scripts.map(s => s.textContent).join('|||');
        }""")

        jobs_from_json = _parse_json_data(raw, query, location)
        if jobs_from_json:
            await browser.close()
            return jobs_from_json

        # Fallback: try CSS selectors (class names change with builds, so try several)
        card_selectors = [
            "[data-test='job-listing']",
            "[class*='JobSearchResult']",
            "[class*='job-listing']",
            "div[class*='styles_result']",
        ]
        cards = []
        for sel in card_selectors:
            cards = await page.query_selector_all(sel)
            if cards:
                break

        if not cards:
            logger.warning("Wellfound: no job cards found with any selector")
            await browser.close()
            return []

        jobs = []
        for card in cards:
            # Extract text content using JS to avoid selector brittleness
            data = await card.evaluate("""el => {
                const links = [...el.querySelectorAll('a[href*="/jobs/"]')];
                const title = el.querySelector('h2,h3,[class*="title"]')?.innerText?.trim() || '';
                const company = el.querySelector('[class*="company"],[class*="startup"]')?.innerText?.trim() || '';
                const loc = el.querySelector('[class*="location"],[class*="loc"]')?.innerText?.trim() || '';
                const url = links[0]?.href || '';
                return { title, company, loc, url };
            }""")
            if not data.get("title"):
                continue
            jobs.append({
                "source": "wellfound",
                "source_job_id": None,
                "title": data["title"],
                "company": data["company"],
                "location": data["loc"],
                "is_remote": is_remote_location(data["loc"], data["title"]),
                "url": data["url"],
                "description": "",
                "experience_level": parse_experience_level(data["title"], ""),
            })
        await browser.close()
        return jobs


def _parse_json_data(raw: str, query: str, location: str) -> list[dict]:
    """Try to extract jobs from embedded page JSON blobs."""
    jobs = []
    for chunk in raw.split("|||"):
        chunk = chunk.strip()
        if not chunk or len(chunk) < 50:
            continue
        try:
            data = json.loads(chunk)
            _walk_json(data, jobs)
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
    # Look for job-like objects with title + url fields
    title = node.get("title") or node.get("name") or ""
    url = node.get("url") or node.get("jobUrl") or node.get("applyUrl") or ""
    company = (
        node.get("startupName") or node.get("company") or
        (node.get("startup") or {}).get("name") or ""
    )
    loc = node.get("remote") or node.get("locationStr") or node.get("location") or ""
    if title and url and "wellfound.com" in url:
        jobs.append({
            "source": "wellfound",
            "source_job_id": str(node.get("id", "")),
            "title": title,
            "company": company,
            "location": loc,
            "is_remote": "remote" in str(loc).lower() or bool(node.get("remote")),
            "url": url,
            "description": node.get("description") or "",
            "experience_level": parse_experience_level(title, ""),
        })
        return
    for v in node.values():
        _walk_json(v, jobs, depth + 1)


async def fetch(query: str, location: str) -> list[dict]:
    try:
        return await _scrape(query, location)
    except Exception as exc:
        logger.error("Wellfound scraper error: %s", exc)
        return []
