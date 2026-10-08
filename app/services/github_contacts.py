"""
Real named engineers, out of a company's GitHub organisation.

Where Hunter is built for B2B sales and covers recruiters worst, GitHub covers
exactly the people worth asking for a referral — and it covers them with a name,
a public profile, and often an email, a blog, or an X handle, for free.

The whole thing hinges on not guessing the wrong org. `stripe` the payments
company and `stripe` some unrelated user's org look identical by name, so an org
is only accepted when something ties it back to the employer: its `blog` points
at the company's domain, its email does, or the slug matches the domain exactly.
An unverified org is discarded rather than guessed at.

Unauthenticated GitHub allows 60 requests an hour, which one fetch cycle blows
through instantly; with GITHUB_TOKEN it is 5,000. Without a token this module
stays off rather than burning the budget and failing halfway.
"""

import logging
import re
from urllib.parse import urlparse

import httpx

from app.services.company_domain import company_key, registrable_domain
from app.services.contact_finder import classify_role, split_name

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
TIMEOUT = 12.0

# Orgs everyone contributes to but nobody is employed by — a match here says
# nothing about who works where.
_GENERIC_ORGS = {
    "opensource", "open-source", "community", "users", "developers", "public",
    "oss", "labs", "org", "team", "engineering",
}

_NOREPLY_RE = re.compile(r"users\.noreply\.github\.com$", re.IGNORECASE)


def _get(path: str, token: str, params: dict | None = None):
    """One GitHub API call, returning parsed JSON or None. Never raises."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "jobapp/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = httpx.get(f"{GITHUB_API}{path}", headers=headers, params=params or {},
                         timeout=TIMEOUT, follow_redirects=True)
    except Exception as exc:
        from app.services.networking import source_issue
        source_issue(f"GitHub request failed: {type(exc).__name__}")
        logger.error("github %s failed: %s", path, exc)
        return None
    if resp.status_code == 404:
        return None
    if resp.status_code == 403 and "rate limit" in (resp.text or "").lower():
        from app.services.networking import source_issue
        source_issue("GitHub rate limit reached")
        logger.warning("github rate limit reached — set GITHUB_TOKEN to raise it")
        return None
    if resp.status_code >= 400:
        from app.services.networking import source_issue
        source_issue(f"GitHub HTTP {resp.status_code}")
        logger.warning("github %s returned %s", path, resp.status_code)
        return None
    try:
        return resp.json()
    except Exception:
        return None


def _org_matches_company(org: dict, company: str, domain: str) -> bool:
    """
    Whether this org demonstrably belongs to the employer we're looking at.

    Require a published website or email at the employer's actual domain.
    A matching display name or slug is only a search candidate. Parse hostnames
    so a company name in an unrelated URL's path or subdomain cannot qualify.
    """
    if not org:
        return False
    login = (org.get("login") or "").lower()
    if login in _GENERIC_ORGS:
        return False

    domain = registrable_domain(domain or "")
    published = [
        (org.get(field) or "").strip().lower() for field in ("blog", "email")
    ]
    published = [v for v in published if v]

    hosts = []
    for value in published:
        host = value.rsplit("@", 1)[-1] if "@" in value and "/" not in value else urlparse(value if "://" in value else f"https://{value}").hostname
        if host:
            hosts.append(registrable_domain(host))
    if domain and domain in hosts:
        return True

    # Names and slugs are discovery candidates, not company ownership evidence.
    return False


def find_org(company: str, domain: str, token: str) -> str | None:
    """
    The company's GitHub org login, or None when nothing verifiable turns up.

    Tries the two obvious slugs directly (cheap), then falls back to org search.
    Every candidate must pass `_org_matches_company` — a plausible name is not
    enough, because acting on the wrong org means emailing total strangers.
    """
    candidates: list[str] = []
    domain_slug = registrable_domain(domain or "").split(".")[0] if domain else ""
    if domain_slug:
        candidates.append(domain_slug)
    key = company_key(company)
    if key and key not in candidates:
        candidates.append(key)

    for slug in candidates:
        org = _get(f"/orgs/{slug}", token)
        if org and _org_matches_company(org, company, domain):
            return org.get("login")

    results = _get("/search/users", token, {"q": f"{company} type:org", "per_page": 5})
    for item in (results or {}).get("items", [])[:5]:
        org = _get(f"/orgs/{item.get('login')}", token)
        if org and _org_matches_company(org, company, domain):
            return org.get("login")

    logger.info("github: no verifiable org for %r (domain %r)", company, domain)
    return None


def _contact_from_user(user: dict, company: str, domain: str) -> dict | None:
    """Normalize a GitHub user into the shape discovery merges on."""
    name = (user.get("name") or "").strip()
    if not name or " " not in name:
        # A handle or a mononym isn't enough to address someone properly, and a
        # message opening "Hi octocat42" is worse than no message.
        return None

    email = (user.get("email") or "").strip().lower() or None
    if email and _NOREPLY_RE.search(email):
        email = None  # GitHub's masked address bounces by design

    bio = (user.get("bio") or "").strip()
    title = bio[:120] if bio else None
    first, last = split_name(name)
    twitter = (user.get("twitter_username") or "").strip() or None

    return {
        "name": name,
        "first_name": first or None,
        "last_name": last or None,
        "title": title,
        "department": None,
        "role": classify_role(bio) if bio else "engineer",
        "email": email,
        "email_status": "unverified" if email else "unknown",
        # Published by the person themselves on their own profile — as good as a
        # posting's address, and better than anything a pattern derives.
        "email_confidence": 75 if email else 0,
        "twitter": twitter,
        "profile_url": user.get("html_url") or None,
        "domain": domain or None,
        "source": "github",
        "evidence": {"sources": [{"uri": user.get("html_url"), "kind": "public_profile"}],
                     "employment_status": "unconfirmed", "claimed_company": user.get("company")},
    }


def github_contacts(company: str, domain: str, token: str, limit: int = 5) -> list[dict]:
    """
    Public members of the company's GitHub org, as contact dicts.

    Only members with a real full name survive, and only public members are
    visible at all — plenty of orgs hide membership, in which case this returns
    nothing, which is the correct answer rather than a failure.
    """
    if not token:
        logger.info("github contacts skipped — no GITHUB_TOKEN configured")
        return []
    if not company:
        return []

    org = find_org(company, domain, token)
    if not org:
        return []

    members = _get(f"/orgs/{org}/public_members", token, {"per_page": 30}) or []
    contacts: list[dict] = []
    for member in members:
        if len(contacts) >= limit:
            break
        login = member.get("login")
        if not login:
            continue
        user = _get(f"/users/{login}", token)
        if not user:
            continue
        contact = _contact_from_user(user, company, domain)
        if contact:
            contacts.append(contact)

    logger.info("github: %d contact(s) from org %s", len(contacts), org)
    return contacts
