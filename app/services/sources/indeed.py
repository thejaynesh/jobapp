import logging

from app.services.sources.base import parse_experience_level
from app.services.sources.playwright_base import (
    CONTEXT_OPTIONS,
    LAUNCH_OPTIONS,
    encode,
    is_remote_location,
    safe_get_attribute,
    safe_inner_text,
)

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.indeed.com"


async def _scrape(query: str, location: str) -> list[dict]:
    from playwright.async_api import async_playwright

    url = f"{_BASE_URL}/jobs?q={encode(query)}&l={encode(location)}&fromage=7"
    async with async_playwright() as p:
        browser = await p.chromium.launch(**LAUNCH_OPTIONS)
        context = await browser.new_context(**CONTEXT_OPTIONS)
        page = await context.new_page()
        try:
            await page.goto(url, timeout=30000, wait_until="domcontentloaded")
            # Try multiple selector patterns for current Indeed DOM
            await page.wait_for_selector(
                "div.job_seen_beacon, [data-testid='slider_item'], .resultContent, [data-jk]",
                timeout=12000,
            )
        except Exception as exc:
            logger.warning("Indeed: page load failed: %s", exc)
            await browser.close()
            return []

        cards = await page.query_selector_all(
            "div.job_seen_beacon, li.css-5lfssm"
        )
        if not cards:
            cards = await page.query_selector_all("[data-jk]")

        jobs = []
        for card in cards:
            title = await safe_inner_text(
                card,
                "h2.jobTitle span[title]",
                "h2.jobTitle a span",
                "h2.jobTitle span",
                "[data-testid='jobTitle']",
            )
            company = await safe_inner_text(
                card,
                "[data-testid='company-name']",
                "span.companyName",
                ".css-1ioi40n",
            )
            loc = await safe_inner_text(
                card,
                "[data-testid='text-location']",
                "div.companyLocation",
                ".css-1p0sjhy",
            )
            # href may be relative like /pagead/... or /rc/clk...
            href = await safe_get_attribute(card, "h2.jobTitle a", "href")
            if not href:
                href = await safe_get_attribute(card, "a[data-jk]", "href")
            if href and not href.startswith("http"):
                href = _BASE_URL + href

            job_id = await safe_get_attribute(card, "[data-jk]", "data-jk")
            if not title:
                continue
            jobs.append({
                "source": "indeed",
                "source_job_id": job_id or None,
                "title": title,
                "company": company,
                "location": loc,
                "is_remote": is_remote_location(loc, title),
                "url": href or "",
                "description": "",
                "experience_level": parse_experience_level(title, ""),
            })
        await browser.close()
        return jobs


async def fetch(query: str, location: str) -> list[dict]:
    try:
        return await _scrape(query, location)
    except Exception as exc:
        logger.error("Indeed scraper error: %s", exc)
        return []
