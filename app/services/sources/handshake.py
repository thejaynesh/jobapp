import logging

from app.services.sources.base import parse_experience_level
from app.services.sources.playwright_base import (
    LAUNCH_OPTIONS,
    is_remote_location,
    safe_get_attribute,
    safe_inner_text,
)

logger = logging.getLogger(__name__)


async def _scrape(session_cookie: str, query: str, location: str) -> list[dict]:
    from playwright.async_api import async_playwright

    url = f"https://joinhandshake.com/stu/postings?search[query]={query}"
    async with async_playwright() as p:
        browser = await p.chromium.launch(**LAUNCH_OPTIONS)
        context = await browser.new_context()
        await context.add_cookies([{
            "name": "_handshake_session",
            "value": session_cookie,
            "domain": "joinhandshake.com",
            "path": "/",
        }])
        page = await context.new_page()
        try:
            await page.goto(url, timeout=30000)
            await page.wait_for_selector(".posting-listing-item", timeout=10000)
        except Exception as exc:
            logger.warning("Handshake: page load failed: %s", exc)
            await browser.close()
            return []

        cards = await page.query_selector_all(".posting-listing-item")
        jobs = []
        for card in cards:
            title = await safe_inner_text(card, ".posting-listing-title")
            company = await safe_inner_text(card, ".posting-listing-company")
            loc = await safe_inner_text(card, ".posting-listing-location")
            job_url = await safe_get_attribute(card, "a", "href")
            if not title:
                continue
            jobs.append({
                "source": "handshake",
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


async def fetch(session_cookie: str, query: str, location: str) -> list[dict]:
    try:
        return await _scrape(session_cookie, query, location)
    except Exception as exc:
        logger.error("Handshake scraper error: %s", exc)
        return []
