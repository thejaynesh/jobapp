import logging

from app.services.sources.playwright_base import (
    CONTEXT_OPTIONS,
    LAUNCH_OPTIONS,
    encode,
    is_remote_location,
    safe_get_attribute,
    safe_inner_text,
)

logger = logging.getLogger(__name__)

# LinkedIn guest jobs API — no login required, returns stable HTML fragments
_GUEST_API = (
    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    "?keywords={query}&location={location}&start=0"
)


async def _scrape(session_cookie: str, query: str, location: str) -> list[dict]:
    from playwright.async_api import async_playwright

    url = _GUEST_API.format(query=encode(query), location=encode(location))
    async with async_playwright() as p:
        browser = await p.chromium.launch(**LAUNCH_OPTIONS)
        context = await browser.new_context(**CONTEXT_OPTIONS)
        if session_cookie:
            await context.add_cookies([{
                "name": "li_at",
                "value": session_cookie,
                "domain": ".linkedin.com",
                "path": "/",
            }])
        page = await context.new_page()
        try:
            await page.goto(url, timeout=30000, wait_until="domcontentloaded")
            await page.wait_for_selector(
                "li, .base-card, .job-search-card", timeout=10000
            )
        except Exception as exc:
            logger.warning("LinkedIn: page load failed: %s", exc)
            await browser.close()
            return []

        cards = await page.query_selector_all(
            "li.occludable-update, div.base-card, li[class*='job-search']"
        )
        if not cards:
            cards = await page.query_selector_all("li")

        jobs = []
        for card in cards:
            title = await safe_inner_text(
                card,
                "h3.base-search-card__title",
                ".job-search-card__title",
                "h3",
            )
            company = await safe_inner_text(
                card,
                "h4.base-search-card__subtitle",
                ".job-search-card__company-name",
                "h4",
            )
            loc = await safe_inner_text(
                card,
                "span.job-search-card__location",
                ".base-search-card__metadata span",
            )
            job_url = await safe_get_attribute(card, "a.base-card__full-link", "href")
            if not job_url:
                job_url = await safe_get_attribute(card, "a[href*='/jobs/view/']", "href")
            if not title or not job_url:
                continue
            jobs.append({
                "source": "linkedin",
                "source_job_id": None,
                "title": title,
                "company": company,
                "location": loc,
                "is_remote": is_remote_location(loc, title),
                "url": job_url.split("?")[0],
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
