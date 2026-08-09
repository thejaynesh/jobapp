"""
Mining a company's own team page for people.

Companies publish exactly what outreach needs on /team or /about — names, job
titles, and very often a link to each person's LinkedIn. Parsing arbitrary
marketing HTML into a reliable list of humans is hopeless in general, so this
only reads the three things that carry an unambiguous signal:

  linkedin.com/in/… links  the highest-value find: a real profile to message
  mailto: addresses        a real address, published deliberately
  JSON-LD Person entries   schema.org markup, when a site bothers to emit it

Anything else on the page is ignored. A thin, correct list beats a long list
built from guessing which <h3> was a name.

Yield is strongly size-dependent: small and mid-size companies list their teams,
household names almost never do.
"""

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

import httpx

from app.services.company_domain import EMAIL_RE, FREE_EMAIL_DOMAINS, registrable_domain
from app.services.contact_finder import classify_role, name_from_local_part, split_name

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
}
TIMEOUT = 10.0

# Cheapest-signal-first, and capped: a company that lists its team does so on one
# of the first couple of these, so trying twenty paths only costs latency.
TEAM_PATHS = ("/team", "/about", "/about-us", "/people", "/leadership", "/company/team")

_LINKEDIN_PROFILE_RE = re.compile(
    r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/([A-Za-z0-9\-_%]+)", re.IGNORECASE
)
_MAILTO_RE = re.compile(r"mailto:([^\"'>?\s]+)", re.IGNORECASE)
_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")

# Slugs that are a company page or a share widget rather than a person.
_NON_PERSON_SLUGS = {"company", "school", "showcase", "shareArticle", "sharing"}


def _fetch(url: str) -> str:
    try:
        resp = httpx.get(url, headers=_HEADERS, timeout=TIMEOUT, follow_redirects=True)
        if resp.status_code >= 400:
            return ""
        # A team page is HTML; anything else is a redirect to a download or an API.
        if "html" not in resp.headers.get("content-type", "").lower():
            return ""
        return resp.text or ""
    except Exception as exc:
        logger.debug("team page fetch failed for %s: %s", url, exc)
        return ""


def _name_from_slug(slug: str) -> str:
    """`jane-doe-8b12a4` -> `Jane Doe`. Empty when the slug isn't name-shaped."""
    parts = [p for p in re.split(r"[-_]+", slug or "") if p]
    # LinkedIn appends a hash to disambiguate; drop trailing junk tokens.
    words = [p for p in parts if p.isalpha() and len(p) > 1]
    if len(words) < 2:
        return ""
    return " ".join(w.capitalize() for w in words[:2])


def _people_from_jsonld(html: str) -> list[dict]:
    """schema.org Person entries, which carry a name and usually a jobTitle."""
    found: list[dict] = []
    for block in _JSONLD_RE.findall(html or ""):
        try:
            data = json.loads(block.strip())
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            for node in ([item] + list(item.get("@graph") or [])):
                if not isinstance(node, dict) or node.get("@type") != "Person":
                    continue
                name = (node.get("name") or "").strip()
                if not name or " " not in name:
                    continue
                found.append({
                    "name": name,
                    "title": (node.get("jobTitle") or "").strip() or None,
                    "email": (node.get("email") or "").strip().lower() or None,
                })
    return found


def people_from_html(html: str, domain: str, page_url: str = "") -> list[dict]:
    """
    Contact dicts for every person the page identifies unambiguously.

    Exposed separately from fetching so the parsing is testable against fixed
    markup rather than against whatever a live site is serving today.
    """
    domain = registrable_domain(domain or "")
    records: list[dict] = []
    by_key: dict[str, dict] = {}

    def merge(candidate: dict) -> None:
        """
        Fold a find into an existing person, or start a new one.

        One person is routinely found twice on the same page — once as a profile
        link and once as a published address or a schema.org block — so a record
        is looked up by every identifier it carries, not just the first.
        """
        keys = [
            f"email:{(candidate.get('email') or '').lower()}" if candidate.get("email") else "",
            f"li:{(candidate.get('linkedin_url') or '').lower()}" if candidate.get("linkedin_url") else "",
            f"name:{(candidate.get('name') or '').lower().strip()}" if candidate.get("name") else "",
        ]
        keys = [k for k in keys if k]
        if not keys:
            return

        existing = next((by_key[k] for k in keys if k in by_key), None)
        if existing is None:
            existing = candidate
            records.append(existing)
        else:
            for field, value in candidate.items():
                if value and not existing.get(field):
                    existing[field] = value

        # Re-index under everything the merged record now answers to, so a later
        # find matching on any identifier lands on it.
        for field, prefix in (("email", "email"), ("linkedin_url", "li"), ("name", "name")):
            value = existing.get(field)
            if value:
                by_key[f"{prefix}:{str(value).lower().strip()}"] = existing

    for slug in _LINKEDIN_PROFILE_RE.findall(html or ""):
        if slug in _NON_PERSON_SLUGS:
            continue
        name = _name_from_slug(slug)
        first, last = split_name(name)
        merge({
            "name": name or None,
            "first_name": first or None,
            "last_name": last or None,
            "title": None,
            "role": "unknown",
            "linkedin_url": f"https://www.linkedin.com/in/{slug}",
            "domain": domain or None,
            "source": "team_page",
            "email_status": "unknown",
            "email_confidence": 0,
        })

    for raw in _MAILTO_RE.findall(html or ""):
        email = raw.split("?")[0].strip().lower()
        if not EMAIL_RE.fullmatch(email):
            continue
        email_domain = registrable_domain(email.rsplit("@", 1)[-1])
        if email_domain in FREE_EMAIL_DOMAINS:
            continue
        if domain and email_domain != domain:
            continue
        name = name_from_local_part(email.split("@")[0])
        first, last = split_name(name)
        merge({
            "name": name or None,
            "first_name": first or None,
            "last_name": last or None,
            "title": None,
            "role": classify_role(email=email),
            "email": email,
            # Published on the company's own site for people to use.
            "email_status": "unverified",
            "email_confidence": 70,
            "domain": email_domain,
            "source": "team_page",
        })

    for person in _people_from_jsonld(html):
        first, last = split_name(person["name"])
        merge({
            "name": person["name"],
            "first_name": first or None,
            "last_name": last or None,
            "title": person["title"],
            "role": classify_role(person["title"] or ""),
            "email": person["email"],
            "email_status": "unverified" if person["email"] else "unknown",
            "email_confidence": 70 if person["email"] else 0,
            "domain": domain or None,
            "source": "team_page",
        })

    return [c for c in records if c.get("name") or c.get("email")]


def team_page_contacts(domain: str, limit: int = 5, workers: int = 3) -> list[dict]:
    """
    People listed on the company's own site.

    The candidate paths are fetched concurrently — most of them 404, and doing
    that serially spends six timeouts to learn nothing.
    """
    domain = registrable_domain(domain or "")
    if not domain:
        return []

    urls = [urljoin(f"https://{domain}", path) for path in TEAM_PATHS]
    found: list[dict] = []
    seen: set[str] = set()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for url, html in zip(urls, pool.map(_fetch, urls)):
            if not html:
                continue
            for contact in people_from_html(html, domain, url):
                key = (contact.get("email") or contact.get("linkedin_url") or "").lower()
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                found.append(contact)
            if len(found) >= limit:
                break

    logger.info("team pages: %d contact(s) on %s", len(found), domain)
    return found[:limit]
