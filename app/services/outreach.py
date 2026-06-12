import asyncio
import logging
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.services.matcher import chat_completion

logger = logging.getLogger(__name__)

HUNTER_DOMAIN_SEARCH_URL = "https://api.hunter.io/v2/domain-search"


def extract_domain(url: str) -> str:
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc or ""
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc
    except Exception:
        return ""


def find_email(company_name: str, domain: str, api_key: str) -> str | None:
    if not api_key:
        return None
    try:
        resp = httpx.get(
            HUNTER_DOMAIN_SEARCH_URL,
            params={"domain": domain, "api_key": api_key, "limit": 1},
            timeout=10,
        )
        data = resp.json().get("data", {})
        email = data.get("email")
        return email or None
    except Exception as exc:
        logger.error("find_email error for %s: %s", company_name, exc)
        return None


def find_linkedin_contact(company_name: str, department: str, session_cookie: str) -> dict:
    if not session_cookie:
        return {}
    try:
        from app.services.sources.playwright_base import LAUNCH_OPTIONS

        async def _scrape():
            from playwright.async_api import async_playwright
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(**LAUNCH_OPTIONS)
                context = await browser.new_context()
                await context.add_cookies([{
                    "name": "li_at",
                    "value": session_cookie,
                    "domain": ".linkedin.com",
                    "path": "/",
                }])
                page = await context.new_page()
                search_url = (
                    f"https://www.linkedin.com/search/results/people/"
                    f"?keywords={company_name}+{department}&origin=GLOBAL_SEARCH_HEADER"
                )
                await page.goto(search_url, timeout=15000)
                await page.wait_for_timeout(3000)
                cards = await page.query_selector_all(".entity-result__item")
                if not cards:
                    await browser.close()
                    return {}
                first = cards[0]
                name_el = await first.query_selector(".entity-result__title-text")
                title_el = await first.query_selector(".entity-result__primary-subtitle")
                result = {
                    "name": (await name_el.inner_text()).strip() if name_el else "",
                    "title": (await title_el.inner_text()).strip() if title_el else "",
                    "source": "linkedin",
                }
                await browser.close()
                return result

        return asyncio.run(_scrape())
    except Exception as exc:
        logger.error("find_linkedin_contact error for %s: %s", company_name, exc)
        return {}


def draft_outreach_message(
    profile_data: dict,
    contact_name: str,
    contact_title: str,
    job_title: str,
    company: str,
    api_key: str,
    base_url: str,
    model: str,
) -> str:
    name = profile_data.get("name", "Candidate")
    summary = profile_data.get("narrative", {}).get("summary", "")
    skills = profile_data.get("skills", {})
    skills_flat = [s for cat in skills.values() for s in cat]

    messages = [
        {
            "role": "system",
            "content": (
                "You write short, personalized LinkedIn outreach messages (3-4 sentences). "
                "Professional tone. No generic phrases like 'I hope this message finds you well'. "
                "Mention the specific role and one relevant skill. End with a clear ask."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Write a LinkedIn message from {name} to {contact_name} ({contact_title} at {company}).\n"
                f"Candidate summary: {summary}\n"
                f"Top skills: {', '.join(skills_flat[:5])}\n"
                f"Target role: {job_title} at {company}"
            ),
        },
    ]
    try:
        return chat_completion(messages=messages, api_key=api_key, base_url=base_url, model=model)
    except Exception as exc:
        logger.error("draft_outreach_message error: %s", exc)
        return (
            f"Hi {contact_name}, I came across the {job_title} role at {company} and believe my background "
            f"in {', '.join(skills_flat[:2])} could be a strong fit. Would love to connect and learn more "
            "about the team. Thanks!"
        )


def run_outreach(db, application) -> None:
    from app.models.profile import Profile

    api_key = settings.NVIDIA_NIM_API_KEY
    base_url = settings.NVIDIA_NIM_BASE_URL
    model = settings.NVIDIA_NIM_MODEL
    hunter_key = settings.HUNTER_IO_API_KEY
    linkedin_cookie = settings.LINKEDIN_SESSION_COOKIE

    profile = db.query(Profile).first()
    profile_data = profile.data if profile else {}

    job = application.job
    domain = extract_domain(job.url)

    email = find_email(job.company, domain, hunter_key)
    linkedin_contact = find_linkedin_contact(job.company, "Engineering", linkedin_cookie)

    contact_name = linkedin_contact.get("name", "Hiring Manager")
    contact_title = linkedin_contact.get("title", "Recruiter")

    message = draft_outreach_message(
        profile_data, contact_name, contact_title,
        job.title, job.company, api_key, base_url, model,
    )

    contact_record = {
        "name": contact_name,
        "title": contact_title,
        "email": email,
        "linkedin_source": linkedin_contact.get("source"),
        "message": message,
    }

    existing = list(application.outreach_contacts or [])
    existing.append(contact_record)
    application.outreach_contacts = existing
    db.commit()
