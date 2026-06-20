import logging

from app.services.sources.base import parse_experience_level
from app.services.sources.playwright_base import (
    CONTEXT_OPTIONS,
    LAUNCH_OPTIONS,
    encode,
    is_remote_location,
)

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.dice.com"


async def _scrape(query: str, location: str) -> list[dict]:
    from playwright.async_api import async_playwright

    url = f"{_BASE_URL}/jobs?q={encode(query)}&location={encode(location)}&countryCode=US&radius=30&radiusUnit=mi&pageSize=20&language=en"
    async with async_playwright() as p:
        browser = await p.chromium.launch(**LAUNCH_OPTIONS)
        context = await browser.new_context(**CONTEXT_OPTIONS)
        page = await context.new_page()
        try:
            await page.goto(url, timeout=30000, wait_until="domcontentloaded")
            await page.wait_for_selector("dhi-job-card, [data-cy='card-title-link']", timeout=12000)
        except Exception as exc:
            logger.warning("Dice: page load failed: %s", exc)
            await browser.close()
            return []

        # dhi-job-card is an Angular custom element — extract via JS to reach inner DOM
        job_data = await page.evaluate("""() => {
            const cards = [...document.querySelectorAll('dhi-job-card')];
            return cards.map(card => {
                const titleEl = card.querySelector(
                    'a.card-title-link, [data-cy="card-title-link"], h5 a, a[href*="/job-detail/"]'
                );
                const companyEl = card.querySelector(
                    '.card-company, [data-cy="search-result-company-name"], span[class*="company"]'
                );
                const locEl = card.querySelector(
                    '.search-result-location, [data-cy="card-location"], span[class*="location"]'
                );
                const href = titleEl ? (titleEl.getAttribute('href') || '') : '';
                return {
                    title: titleEl ? titleEl.innerText.trim() : '',
                    company: companyEl ? companyEl.innerText.trim() : '',
                    location: locEl ? locEl.innerText.trim() : '',
                    url: href.startsWith('http') ? href : ('https://www.dice.com' + href),
                };
            });
        }""")

        await browser.close()
        jobs = []
        for d in job_data:
            if not d.get("title"):
                continue
            jobs.append({
                "source": "dice",
                "source_job_id": None,
                "title": d["title"],
                "company": d["company"],
                "location": d["location"],
                "is_remote": is_remote_location(d["location"], d["title"]),
                "url": d["url"],
                "description": "",
                "experience_level": parse_experience_level(d["title"], ""),
            })
        return jobs


async def fetch(query: str, location: str) -> list[dict]:
    try:
        return await _scrape(query, location)
    except Exception as exc:
        logger.error("Dice scraper error: %s", exc)
        return []
