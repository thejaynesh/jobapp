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

_BASE_URL = "https://app.joinhandshake.com"


async def _scrape(session_cookie: str, query: str, location: str) -> list[dict]:
    from playwright.async_api import async_playwright

    url = f"{_BASE_URL}/stu/postings?search%5Bquery%5D={encode(query)}&search%5Bposting_type%5D=job"
    async with async_playwright() as p:
        browser = await p.chromium.launch(**LAUNCH_OPTIONS)
        context = await browser.new_context(**CONTEXT_OPTIONS)
        await context.add_cookies([
            {
                "name": "_handshake_session",
                "value": session_cookie,
                "domain": "app.joinhandshake.com",
                "path": "/",
            },
            # Some Handshake versions use this cookie name
            {
                "name": "handshake_session",
                "value": session_cookie,
                "domain": "app.joinhandshake.com",
                "path": "/",
            },
        ])
        page = await context.new_page()
        try:
            await page.goto(url, timeout=30000, wait_until="domcontentloaded")
            await page.wait_for_load_state("networkidle", timeout=15000)
            await page.wait_for_selector(
                "[data-hook='posting-listing-item'], [data-hook='posting'], "
                ".posting-listing-item, li[class*='posting'], li[class*='job']",
                timeout=10000,
            )
        except Exception as exc:
            logger.warning("Handshake: page load failed: %s", exc)
            await browser.close()
            return []

        # Use JS to extract since Handshake uses React with obfuscated class names
        job_data = await page.evaluate(f"""() => {{
            const base = '{_BASE_URL}';
            const hooks = [
                '[data-hook="posting-listing-item"]',
                '[data-hook="posting"]',
                '.posting-listing-item',
                'li[class*="posting"]',
                'li[class*="JobResult"]',
            ];
            let cards = [];
            for (const sel of hooks) {{
                cards = [...document.querySelectorAll(sel)];
                if (cards.length) break;
            }}
            return cards.map(card => {{
                const titleEl = card.querySelector(
                    '[data-hook="posting-name"], [class*="title"], h3, h4, a[href*="/postings/"]'
                );
                const companyEl = card.querySelector(
                    '[data-hook="posting-employer-name"], [class*="employer"], [class*="company"]'
                );
                const locEl = card.querySelector(
                    '[data-hook="posting-location"], [class*="location"]'
                );
                const href = card.querySelector('a[href*="/postings/"]')?.getAttribute('href') || '';
                return {{
                    title: titleEl ? titleEl.innerText.trim() : '',
                    company: companyEl ? companyEl.innerText.trim() : '',
                    location: locEl ? locEl.innerText.trim() : '',
                    url: href.startsWith('http') ? href : (base + href),
                }};
            }});
        }}""")

        await browser.close()
        jobs = []
        for d in job_data:
            if not d.get("title"):
                continue
            jobs.append({
                "source": "handshake",
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


async def fetch(session_cookie: str, query: str, location: str) -> list[dict]:
    try:
        return await _scrape(session_cookie, query, location)
    except Exception as exc:
        logger.error("Handshake scraper error: %s", exc)
        return []
