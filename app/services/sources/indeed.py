import logging

from app.services.sources.base import parse_experience_level
from app.services.sources.playwright_base import (
    LAUNCH_OPTIONS,
    is_remote_location,
    safe_get_attribute,
    safe_inner_text,
)

logger = logging.getLogger(__name__)


async def _scrape(query: str, location: str) -> list[dict]:
    from playwright.async_api import async_playwright

    url = f"https://www.indeed.com/jobs?q={query}&l={location}"
    async with async_playwright() as p:
        browser = await p.chromium.launch(**LAUNCH_OPTIONS)
        page = await browser.new_page()
        try:
            await page.goto(url, timeout=30000)
            await page.wait_for_selector('[data-testid="slider_item"]', timeout=10000)
        except Exception as exc:
            logger.warning("Indeed: page load failed: %s", exc)
            await browser.close()
            return []

        cards = await page.query_selector_all('[data-testid="slider_item"]')
        jobs = []
        for card in cards:
            title = await safe_inner_text(card, ".jobTitle span")
            company = await safe_inner_text(card, ".companyName")
            loc = await safe_inner_text(card, ".companyLocation")
            job_url = await safe_get_attribute(card, "a.jcs-JobTitle", "href")
            if not title:
                continue
            jobs.append({
                "source": "indeed",
                "source_job_id": None,
                "title": title,
                "company": company,
                "location": loc,
                "is_remote": is_remote_location(loc, title),
                "url": job_url,
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
