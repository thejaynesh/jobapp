"""
Finding people to write to.

Four independent sources, each optional and each degrading to nothing rather
than to an exception:

  hunter       — Hunter.io domain search: real addresses with departments,
                 seniority, and the company's own address pattern
  description  — addresses the posting itself leaks ("email jane@acme.com")
  pattern      — an address derived from a known name plus the company pattern,
                 stored as a guess and never auto-sent
  linkedin     — the people search, for names and titles when there is a session
                 cookie to use (no email, but a profile to message)

Everything returns plain dicts in one shape so `outreach.discover_contacts` can
merge them without caring where each came from.
"""

import asyncio
import logging
import re

import httpx

from app.services.company_domain import (
    EMAIL_RE, FREE_EMAIL_DOMAINS, is_company_domain, registrable_domain,
)

logger = logging.getLogger(__name__)

HUNTER_DOMAIN_SEARCH_URL = "https://api.hunter.io/v2/domain-search"
HUNTER_EMAIL_FINDER_URL = "https://api.hunter.io/v2/email-finder"
HUNTER_EMAIL_VERIFIER_URL = "https://api.hunter.io/v2/email-verifier"

HTTP_TIMEOUT = 15.0

# Role-bearing words in a job title, most specific first — the first hit wins,
# so "engineering recruiter" is a recruiter rather than an engineer.
_ROLE_TITLE_MARKERS: list[tuple[str, tuple[str, ...]]] = [
    ("recruiter", ("recruiter", "recruiting", "recruitment", "talent acquisition",
                   "talent partner", "sourcer", "staffing")),
    ("hiring_manager", ("engineering manager", "hiring manager", "head of engineering",
                        "director of engineering", "team lead", "tech lead",
                        "manager, engineering", "people manager")),
    ("executive", ("chief", "cto", "ceo", "coo", "cfo", "founder", "co-founder",
                   "president", "vp ", "vice president", "head of")),
    ("engineer", ("engineer", "developer", "programmer", "architect", "scientist",
                  "sre", "devops")),
]

# Mailbox names that reach a queue rather than a person.
GENERIC_LOCAL_PARTS = {
    "info", "support", "sales", "contact", "hello", "help", "admin", "press",
    "legal", "billing", "noreply", "no-reply", "donotreply", "marketing",
    "webmaster", "abuse", "privacy", "security", "team", "office", "enquiries",
    "inquiries", "general", "mail", "postmaster", "media", "partnerships",
}

# Generic mailboxes that are still the right place for a job enquiry.
RECRUITING_LOCAL_PARTS = {
    "careers", "career", "jobs", "job", "recruiting", "recruitment", "recruit",
    "hiring", "hire", "talent", "hr", "people", "apply", "applications", "join",
    "work", "workwithus",
}

# Hunter departments, scored by how likely they are to answer a job enquiry.
_DEPARTMENT_SCORES = {
    "hr": 40, "executive": 25, "management": 20, "communication": 10,
    "it": 10, "engineering": 15, "marketing": 0, "sales": -10,
    "finance": -15, "legal": -15, "support": -10,
}

# Ranked by what actually converts, not by who is easiest to identify.
# Referred candidates convert at roughly 30% against 0.1-2% for a cold
# application, and the person who files a referral is a peer engineer — so they
# outrank everyone. Reaching a hiring manager directly is the next best thing
# (about 3x the interview rate of applying alone). Recruiters sit below both:
# their inbox is the most contested in the company and they are gatekeepers
# rather than advocates. A generic mailbox is scored below nothing at all,
# because it is the resume black hole reached by a different route.
_ROLE_SCORES = {
    "engineer": 50, "hiring_manager": 45, "recruiter": 25,
    "executive": 20, "unknown": 0, "generic": -30,
}

# Address shapes to try when we know a name but not the address. Ordered by how
# common they are, so the first guess is the one most likely to land.
_PATTERN_TEMPLATES = [
    "{first}.{last}", "{first}", "{f}{last}", "{first}{last}",
    "{first}_{last}", "{f}.{last}", "{last}{f}", "{first}-{last}",
]

_NAME_CLEAN_RE = re.compile(r"[^a-z]+")


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

def split_name(name: str) -> tuple[str, str]:
    """("Jane Q. Doe") -> ("Jane", "Doe"). Middle names and initials are dropped."""
    parts = [p for p in re.split(r"\s+", (name or "").strip()) if p and not p.endswith(".")]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


def name_from_local_part(local_part: str) -> str:
    """`jane.doe` -> `Jane Doe`. Empty for mailboxes that aren't a person's name."""
    local = (local_part or "").lower()
    if local in GENERIC_LOCAL_PARTS or local in RECRUITING_LOCAL_PARTS:
        return ""
    words = [w for w in re.split(r"[._\-+]+", local) if w and not w.isdigit()]
    # Single-token mailboxes ("jane", "jdoe") are too ambiguous to name someone.
    if len(words) < 2 or any(len(w) < 2 for w in words):
        return ""
    return " ".join(w.capitalize() for w in words[:2])


def classify_role(title: str = "", department: str = "", email: str = "") -> str:
    """Which of CONTACT_ROLES this person is, from whatever we happen to know."""
    text = f"{title or ''} {department or ''}".lower()
    for role, markers in _ROLE_TITLE_MARKERS:
        if any(m in text for m in markers):
            return role
    local = (email or "").split("@")[0].lower()
    if local in RECRUITING_LOCAL_PARTS:
        return "recruiter"
    if local in GENERIC_LOCAL_PARTS:
        return "generic"
    if (department or "").lower() == "hr":
        return "recruiter"
    return "unknown"


def contact_score(contact: dict) -> int:
    """
    How worth writing to this person is. Used to rank a discovery run's finds so
    the per-application cap keeps the best few.
    """
    score = _ROLE_SCORES.get(contact.get("role") or "unknown", 0)
    score += _DEPARTMENT_SCORES.get((contact.get("department") or "").lower(), 0)
    status = contact.get("email_status")
    if status == "verified":
        score += 30
    elif status in ("unverified", "accept_all"):
        score += 15
    elif status == "guessed":
        score += 5
    elif status == "invalid":
        score -= 40
    score += int(contact.get("email_confidence") or 0) // 10
    if contact.get("name"):
        score += 10
    if contact.get("linkedin_url"):
        score += 8
    if not contact.get("email") and not contact.get("linkedin_url"):
        score -= 50
    return score


# ---------------------------------------------------------------------------
# Email patterns
# ---------------------------------------------------------------------------

def apply_pattern(pattern: str, first: str, last: str, domain: str) -> str:
    """
    Render a Hunter-style pattern ("{first}.{last}", "{f}{last}") for a name.

    Returns "" when the pattern needs a part of the name we don't have, which is
    the common case for a single-word name.
    """
    first = _NAME_CLEAN_RE.sub("", (first or "").lower())
    last = _NAME_CLEAN_RE.sub("", (last or "").lower())
    if not pattern or not domain or not first:
        return ""
    values = {"first": first, "last": last, "f": first[:1], "l": last[:1] if last else ""}
    try:
        local = pattern.format(**values)
    except (KeyError, IndexError):
        return ""
    if not local or "{" in local:
        return ""
    # A pattern needing a surname we don't have collapses to something like
    # "jane." or "j" — neither is a real address.
    if not last and any(token in pattern for token in ("{last}", "{l}")):
        return ""
    return f"{local.strip('._-')}@{domain}"


def guess_emails(first: str, last: str, domain: str, pattern: str = "") -> list[str]:
    """Plausible addresses for a named person, best first, no duplicates."""
    if not domain or not first:
        return []
    guesses: list[str] = []
    for template in ([pattern] if pattern else []) + _PATTERN_TEMPLATES:
        email = apply_pattern(template, first, last, domain)
        if email and email not in guesses:
            guesses.append(email)
    return guesses


# ---------------------------------------------------------------------------
# Hunter.io
# ---------------------------------------------------------------------------

def _hunter_get(url: str, params: dict, api_key: str) -> dict:
    """A Hunter call that returns `data` or {} — quota and network errors alike."""
    if not api_key:
        return {}
    try:
        resp = httpx.get(url, params={**params, "api_key": api_key}, timeout=HTTP_TIMEOUT)
        payload = resp.json() if resp.content else {}
    except Exception as exc:
        logger.error("hunter %s failed: %s", url.rsplit("/", 1)[-1], exc)
        return {}
    errors = payload.get("errors")
    if errors:
        detail = "; ".join(e.get("details", "") for e in errors if isinstance(e, dict))
        logger.warning("hunter %s rejected the call: %s", url.rsplit("/", 1)[-1], detail)
        return {}
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def _verifier_status(result: str) -> str:
    return {
        "deliverable": "verified",
        "risky": "accept_all",
        "accept_all": "accept_all",
        "undeliverable": "invalid",
        "unknown": "unverified",
    }.get((result or "").lower(), "unverified")


def hunter_domain_search(domain: str, api_key: str, limit: int = 10) -> dict:
    """Raw Hunter `data` for a domain: `emails`, plus the company `pattern`."""
    if not domain or not api_key:
        return {}
    return _hunter_get(
        HUNTER_DOMAIN_SEARCH_URL,
        {"domain": domain, "limit": max(1, min(limit, 100))},
        api_key,
    )


def hunter_email_finder(domain: str, first: str, last: str, api_key: str) -> dict:
    """Hunter's address for a specific person: {"email", "score"} or {}."""
    if not (domain and first and last and api_key):
        return {}
    data = _hunter_get(
        HUNTER_EMAIL_FINDER_URL,
        {"domain": domain, "first_name": first, "last_name": last},
        api_key,
    )
    email = data.get("email")
    if not email:
        return {}
    return {"email": email, "score": int(data.get("score") or 0)}


def verify_email(email: str, api_key: str) -> dict:
    """
    Check an address exists. Returns {"status", "confidence"}; an unavailable
    verifier leaves the address exactly as trusted as it already was.
    """
    if not email or not api_key:
        return {}
    data = _hunter_get(HUNTER_EMAIL_VERIFIER_URL, {"email": email}, api_key)
    if not data:
        return {}
    return {
        "status": _verifier_status(data.get("result") or data.get("status") or ""),
        "confidence": int(data.get("score") or 0),
    }


def find_email(company_name: str, domain: str, api_key: str) -> str | None:
    """
    The single best address at a company, or None.

    Kept as the simple entry point (and for callers that only want an address).
    Hunter's domain-search returns `data.emails[]`; some responses have carried a
    top-level `data.email` instead, so both shapes are read.
    """
    if not api_key:
        return None
    data = hunter_domain_search(domain, api_key, limit=10)
    if not data:
        return None
    direct = data.get("email")
    if direct:
        return direct
    contacts = hunter_contacts(domain, api_key, limit=10, data=data)
    return contacts[0]["email"] if contacts else None


def hunter_contacts(domain: str, api_key: str, limit: int = 10, data: dict | None = None) -> list[dict]:
    """
    People at a domain, ranked by how likely they are to be useful, best first.

    `data` lets a caller that already paid for a domain search reuse it rather
    than spending a second credit.
    """
    if data is None:
        data = hunter_domain_search(domain, api_key, limit=limit)
    if not data:
        return []

    pattern = data.get("pattern") or ""
    found: list[dict] = []
    for entry in data.get("emails") or []:
        if not isinstance(entry, dict):
            continue
        email = (entry.get("value") or "").strip().lower()
        if not email or "@" not in email:
            continue
        first = (entry.get("first_name") or "").strip()
        last = (entry.get("last_name") or "").strip()
        name = " ".join(p for p in (first, last) if p) or name_from_local_part(
            email.split("@")[0]
        )
        title = (entry.get("position") or "").strip()
        department = (entry.get("department") or "").strip()
        verification = entry.get("verification") or {}
        status = _verifier_status(verification.get("status") or "") if verification else (
            "verified" if entry.get("type") == "personal" else "unverified"
        )
        found.append({
            "name": name or None,
            "first_name": first or None,
            "last_name": last or None,
            "title": title or None,
            "department": department or None,
            "role": classify_role(title, department, email),
            "email": email,
            "email_status": status,
            "email_confidence": int(entry.get("confidence") or 0),
            "linkedin_url": entry.get("linkedin") or None,
            "twitter": entry.get("twitter") or None,
            "phone": entry.get("phone_number") or None,
            "domain": domain,
            "source": "hunter",
            "pattern": pattern,
        })

    found.sort(key=contact_score, reverse=True)
    return found[:limit]


# ---------------------------------------------------------------------------
# The posting itself
# ---------------------------------------------------------------------------

def contacts_from_description(text: str, domain: str = "") -> list[dict]:
    """
    Addresses the job description hands over directly.

    These are the best leads there are — someone wrote them down expecting
    applicants to use them — so anything at the company's own domain counts,
    and a personal address only counts if it isn't a free-mail one.
    """
    seen: set[str] = set()
    found: list[dict] = []
    for match in EMAIL_RE.finditer(text or ""):
        email = match.group().strip().lower().rstrip(".")
        if email in seen:
            continue
        seen.add(email)
        email_domain = registrable_domain(email.rsplit("@", 1)[-1])
        if email_domain in FREE_EMAIL_DOMAINS:
            continue
        if domain and email_domain != registrable_domain(domain) and not is_company_domain(email_domain):
            continue
        local = email.split("@")[0]
        name = name_from_local_part(local)
        first, last = split_name(name)
        found.append({
            "name": name or None,
            "first_name": first or None,
            "last_name": last or None,
            "title": None,
            "department": None,
            "role": classify_role(email=email),
            "email": email,
            # Printed in a posting for exactly this purpose — treat it as real,
            # just not independently confirmed.
            "email_status": "unverified",
            "email_confidence": 70,
            "domain": email_domain,
            "source": "description",
        })
    found.sort(key=contact_score, reverse=True)
    return found


# ---------------------------------------------------------------------------
# LinkedIn people search
# ---------------------------------------------------------------------------

# Anchoring on the profile link rather than on card class names: LinkedIn
# rewrites its cosmetic classes constantly, but a result is always an <a> to
# /in/<slug>, and the visible name is always inside it in an aria-hidden span
# (the sibling text is the screen-reader duplicate). That structure has outlived
# several redesigns; `.entity-result__*` has not.
_PROFILE_ANCHOR = "a[href*='/in/']"
_NAME_IN_ANCHOR = ("span[aria-hidden='true']", "span.t-16", "span")
# Subtitle lives beside the anchor's card; these are ordered newest markup first.
_TITLE_SELECTORS = (
    "div.t-14.t-black.t-normal",
    ".entity-result__primary-subtitle",
    ".entity-result__summary",
)

_NON_PERSON_NAMES = {"linkedin member", "member", "", "linkedin"}


async def _scrape_people(company: str, title_query: str, cookie: str, limit: int) -> list[dict]:
    from playwright.async_api import async_playwright

    from app.services.sources.playwright_base import (
        CONTEXT_OPTIONS, LAUNCH_OPTIONS, describe_page, encode, safe_inner_text,
    )

    keywords = f"{company} {title_query}".strip()
    search_url = (
        "https://www.linkedin.com/search/results/people/"
        f"?keywords={encode(keywords)}&origin=GLOBAL_SEARCH_HEADER"
    )

    results: list[dict] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**LAUNCH_OPTIONS)
        try:
            context = await browser.new_context(**CONTEXT_OPTIONS)
            await context.add_cookies([{
                "name": "li_at", "value": cookie, "domain": ".linkedin.com", "path": "/",
            }])
            page = await context.new_page()
            await page.goto(search_url, timeout=30000, wait_until="domcontentloaded")
            # Results render client-side; wait for the anchors rather than a
            # fixed sleep, so a fast page isn't penalised and a slow one isn't cut off.
            try:
                await page.wait_for_selector(_PROFILE_ANCHOR, timeout=12000)
            except Exception:
                pass

            anchors = await page.query_selector_all(_PROFILE_ANCHOR)
            if not anchors:
                # "No results" and "you have been challenged" look identical from
                # an empty selector, and they need opposite fixes — one needs new
                # selectors, the other needs to stop and back off.
                logger.warning(
                    "linkedin people search for %r found nobody: %s",
                    keywords, await describe_page(page),
                )
                return []

            seen: set[str] = set()
            for anchor in anchors:
                if len(results) >= limit:
                    break
                href = (await anchor.get_attribute("href")) or ""
                profile_url = href.split("?")[0]
                if not profile_url or "/in/" not in profile_url or profile_url in seen:
                    continue

                name = (await safe_inner_text(anchor, *_NAME_IN_ANCHOR)).strip()
                if not name:
                    name = ((await anchor.inner_text()) or "").strip().split("\n")[0]
                # The same profile is linked from the photo and the name; the
                # photo anchor has no text, so a nameless hit is a duplicate.
                if name.lower() in _NON_PERSON_NAMES:
                    continue
                seen.add(profile_url)

                position = ""
                card = await anchor.evaluate_handle("el => el.closest('li') || el.parentElement")
                if card:
                    element = card.as_element()
                    if element:
                        position = await safe_inner_text(element, *_TITLE_SELECTORS)

                first, last = split_name(name)
                results.append({
                    "name": name,
                    "first_name": first or None,
                    "last_name": last or None,
                    "title": position.strip() or None,
                    "department": None,
                    "role": classify_role(position),
                    "linkedin_url": profile_url if profile_url.startswith("http")
                    else f"https://www.linkedin.com{profile_url}",
                    "source": "linkedin",
                })
        finally:
            await browser.close()
    return results


def find_linkedin_contacts(
    company_name: str, titles: list[str] | None, session_cookie: str, limit: int = 3
) -> list[dict]:
    """
    People at a company whose titles match, via the logged-in people search.

    Needs LINKEDIN_SESSION_COOKIE and costs a browser launch, so callers gate it
    behind OUTREACH_USE_LINKEDIN. Any failure — no cookie, a redesign, a
    challenge page — comes back as an empty list.
    """
    if not session_cookie or not company_name:
        return []
    query = " OR ".join(t.strip() for t in (titles or []) if t.strip())
    try:
        return asyncio.run(_scrape_people(company_name, query, session_cookie, limit))
    except Exception as exc:
        logger.error("find_linkedin_contacts error for %s: %s", company_name, exc)
        return []


def find_linkedin_contact(company_name: str, department: str, session_cookie: str) -> dict:
    """The single best LinkedIn match, or {}."""
    contacts = find_linkedin_contacts(company_name, [department], session_cookie, limit=1)
    return contacts[0] if contacts else {}
