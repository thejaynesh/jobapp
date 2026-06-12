import logging

from app.services.sources.playwright_base import (
    LAUNCH_OPTIONS,
    is_remote_location,
    safe_get_attribute,
    safe_inner_text,
)

logger = logging.getLogger(__name__)


async def _scrape(session_cookie: str, query: str, location: str) -> list[dict]:
    from playwright.async_api import async_playwright

    url = f"https://www.linkedin.com/jobs/search/?keywords={query}&location={location}"
    async with async_playwright() as p:
        browser = await p.chromium.launch(**LAUNCH_OPTIONS)
        context = await browser.new_context()
        await context.add_cookies([{
            "name": "li_at",
            "value": session_cookie,
            "domain": ".linkedin.com",
            "path": "/",
        }])
        page = await context.new_page()
        try:
            await page.goto(url, timeout=30000)
            await page.wait_for_selector(".base-card", timeout=10000)
        except Exception as exc:
            logger.warning("LinkedIn: page load failed: %s", exc)
            await browser.close()
            return []

        cards = await page.query_selector_all(".base-card")
        jobs = []
        for card in cards:
            title = await safe_inner_text(card, ".base-search-card__title")
            company = await safe_inner_text(card, ".base-search-card__subtitle")
            loc = await safe_inner_text(card, ".job-search-card__location")
            job_url = await safe_get_attribute(card, "a.base-card__full-link", "href")
            if not title or not job_url:
                continue
            jobs.append({
                "source": "linkedin",
                "source_job_id": None,
                "title": title,
                "company": company,
                "location": loc,
                "is_remote": is_remote_location(loc, title),
                "url": job_url,
                "description": "",
                "experience_level": "mid",
            })
        await browser.close()
        return jobs


async def fetch(session_cookie: str, query: str, location: str) -> list[dict]:
    try:
        return await _scrape(session_cookie, query, location)
    except Exception as exc:
        logger.error("LinkedIn scraper error: %s", exc)
        return []
