"""
Pre-built LinkedIn searches, so messaging someone is one click rather than ten.

Nothing here talks to LinkedIn. It composes URLs that open a filtered people
search in the user's own browser, where they are already logged in — which is
both the most reliable way to reach a real profile (no bot detection to trip,
nothing to block) and the only way that doesn't put their account at risk.

Two kinds of link:
  company-level — "recruiters at Acme", "engineering managers at Acme", and the
                  alumni angle, which is the single best-answered cold approach
                  there is: someone from your university already works there
  person-level  — a direct profile when we have one, otherwise a name search
                  scoped to the company
"""

import logging
import re
from urllib.parse import quote_plus, urlparse

logger = logging.getLogger(__name__)

PEOPLE_SEARCH = "https://www.linkedin.com/search/results/people/?keywords={q}"
COMPANY_PEOPLE = "https://www.linkedin.com/company/{slug}/people/"
COMPANY_PEOPLE_FILTERED = "https://www.linkedin.com/company/{slug}/people/?keywords={q}"

# Search phrasings per angle. LinkedIn's keyword search is an OR of terms rather
# than a strict boolean, so these are kept short — a long query dilutes the match
# instead of narrowing it.
ANGLES: list[tuple[str, str, str]] = [
    ("recruiter", "recruiter", "Recruiters"),
    ("hiring_manager", "engineering manager", "Engineering managers"),
    ("engineer", "software engineer", "Engineers"),
]

_LINKEDIN_COMPANY_RE = re.compile(
    r"linkedin\.com/company/([A-Za-z0-9\-_.]+)", re.IGNORECASE
)
_SLUG_CLEAN_RE = re.compile(r"[^a-z0-9]+")

# Legal suffixes to drop when guessing a slug. LinkedIn slugs are usually the
# trading name — "stripe", not "stripe-inc".
_SLUG_NOISE = {"inc", "llc", "ltd", "limited", "corp", "corporation", "co",
               "company", "gmbh", "plc", "the", "group", "holdings"}


def company_slug(company: str, description: str = "", url: str = "") -> str:
    """
    The company's LinkedIn slug.

    A slug quoted in the posting is worth far more than one derived from the
    name — plenty of companies trade under a name that isn't their slug — so
    the text is mined first and the guess is only a fallback.
    """
    for text in (description or "", url or ""):
        match = _LINKEDIN_COMPANY_RE.search(text)
        if match:
            return match.group(1).rstrip("/").lower()

    tokens = _SLUG_CLEAN_RE.sub(" ", (company or "").lower()).split()
    kept = [t for t in tokens if t not in _SLUG_NOISE] or tokens
    return "-".join(kept)


def people_search_url(*terms: str) -> str:
    """A global people search for the given terms."""
    query = " ".join(t.strip() for t in terms if t and t.strip())
    return PEOPLE_SEARCH.format(q=quote_plus(query)) if query else ""


def company_people_url(slug: str, keywords: str = "") -> str:
    """
    The company's own People tab, optionally filtered.

    Better than a global search when the slug is known: everyone on it verifiably
    works there, where a keyword search mixes in ex-employees and name collisions.
    """
    if not slug:
        return ""
    if keywords:
        return COMPANY_PEOPLE_FILTERED.format(slug=slug, q=quote_plus(keywords))
    return COMPANY_PEOPLE.format(slug=slug)


def profile_url_for(name: str, company: str = "") -> str:
    """A search that should land on one specific person's profile."""
    if not name:
        return ""
    return people_search_url(name, company)


def _school_names(profile_data: dict, limit: int = 2) -> list[str]:
    schools = []
    for entry in (profile_data.get("education") or [])[:limit]:
        school = (entry.get("school") or entry.get("institution") or "").strip()
        if school:
            schools.append(school)
    return schools


def company_links(company: str, profile_data: dict | None = None,
                  description: str = "", url: str = "") -> list[dict]:
    """
    The set of searches worth running for one employer, best angle first.

    Returns [{"label", "url", "hint", "primary"}]. The alumni links come first
    and are the only ones flagged `primary`: a shared university is the
    strongest reason a stranger has to reply, and the UI leans on that flag to
    say so rather than presenting five equal-weight buttons.
    """
    slug = company_slug(company, description, url)
    links: list[dict] = []

    for school in _school_names(profile_data or {}):
        links.append({
            "label": f"{school} alumni at {company}",
            "url": company_people_url(slug, school) or people_search_url(company, school),
            "hint": "Shared university — the best-answered cold message there is",
            "primary": True,
        })

    for _, query, label in ANGLES:
        links.append({
            "label": f"{label} at {company}",
            "url": company_people_url(slug, query) or people_search_url(company, query),
            "hint": "",
            "primary": False,
        })

    links.append({
        "label": f"Everyone at {company}",
        "url": company_people_url(slug),
        "hint": "The company's People tab",
        "primary": False,
    })
    return [link for link in links if link["url"]]


def contact_link(contact) -> str:
    """
    The best LinkedIn URL for one person: their profile if we have it, otherwise
    a search for them by name at their company.
    """
    existing = getattr(contact, "linkedin_url", None)
    if existing:
        return existing
    name = getattr(contact, "name", None)
    if not name:
        return ""
    return profile_url_for(name, getattr(contact, "company", "") or "")


def is_profile_url(url: str) -> bool:
    """Whether a URL points at an individual's profile rather than a search."""
    try:
        parsed = urlparse(url or "")
    except Exception:
        return False
    return "linkedin.com" in (parsed.netloc or "").lower() and "/in/" in (parsed.path or "")
