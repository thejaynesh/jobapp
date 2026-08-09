"""
Working out which internet domain an employer actually owns.

Outreach needs this before anything else: Hunter is queried by domain, and every
email pattern we guess hangs off it. The job's own URL is usually no help — it
points at boards.greenhouse.io or an Indeed redirect, not the company — so this
module works through the places the real domain does show up, cheapest first.
"""

import logging
import re
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# Hosts that belong to an applicant tracking system, not to the employer.
ATS_HOSTS = {
    "greenhouse.io", "boards.greenhouse.io", "job-boards.greenhouse.io",
    "lever.co", "jobs.lever.co", "ashbyhq.com", "jobs.ashbyhq.com",
    "smartrecruiters.com", "jobs.smartrecruiters.com", "workable.com",
    "apply.workable.com", "recruitee.com", "myworkdayjobs.com", "myworkdaysite.com",
    "workday.com", "icims.com", "taleo.net", "successfactors.com", "successfactors.eu",
    "bamboohr.com", "jazzhr.com", "applytojob.com", "breezy.hr", "teamtailor.com",
    "personio.de", "personio.com", "rippling.com", "paylocity.com", "dayforcehcm.com",
    "oraclecloud.com", "eightfold.ai", "jobvite.com", "hire.withgoogle.com",
    "workforcenow.adp.com", "adp.com", "paycomonline.net", "ultipro.com",
    "silkroad.com", "brassring.com", "avature.net", "gr8people.com", "phenompeople.com",
    "pinpointhq.com", "polymer.co", "trakstar.com", "hire.lever.co", "join.com",
    "workatastartup.com", "getro.com", "consider.com", "comeet.com", "freshteam.com",
    "zohorecruit.com", "keka.com", "darwinbox.com", "hirehive.com", "manatal.com",
}

# Job boards, aggregators, and other places a posting can live that are not the
# employer either.
AGGREGATOR_HOSTS = {
    "linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com", "dice.com",
    "monster.com", "careerbuilder.com", "simplyhired.com", "adzuna.com", "adzuna.co.uk",
    "jooble.org", "careerjet.com", "themuse.com", "remotive.com", "remotive.io",
    "weworkremotely.com", "remoteok.com", "remoteok.io", "wellfound.com", "angel.co",
    "jobicy.com", "himalayas.app", "arbeitnow.com", "findwork.dev", "builtin.com",
    "joinhandshake.com", "otta.com", "welcometothejungle.com", "hnhiring.com",
    "news.ycombinator.com", "ycombinator.com", "reed.co.uk", "totaljobs.com",
    "seek.com.au", "naukri.com", "stackoverflow.com", "github.com", "google.com",
    "bing.com", "facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com",
    "medium.com", "notion.site", "docs.google.com", "forms.gle", "bit.ly", "lnkd.in",
    "t.co", "tinyurl.com", "wikipedia.org", "crunchbase.com", "levels.fyi", "jobright.ai",
    "talent.com", "jobs2careers.com", "neuvoo.com", "upwork.com", "toptal.com",
}

NON_COMPANY_HOSTS = ATS_HOSTS | AGGREGATOR_HOSTS

# Addresses at these domains are personal, never an employer's.
FREE_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "hotmail.com",
    "outlook.com", "live.com", "msn.com", "aol.com", "icloud.com", "me.com",
    "mac.com", "protonmail.com", "proton.me", "gmx.com", "gmx.de", "mail.com",
    "yandex.com", "zoho.com", "fastmail.com", "hey.com", "qq.com", "163.com",
    "example.com", "example.org", "sentry.io", "wixpress.com",
}

# Two-label public suffixes we actually see, so careers.acme.co.uk resolves to
# acme.co.uk rather than co.uk. Not exhaustive by design — a full public suffix
# list would be a dependency for a handful of extra countries.
MULTIPART_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "co.jp", "or.jp", "ne.jp",
    "co.in", "net.in", "org.in", "com.au", "net.au", "org.au", "edu.au",
    "com.br", "com.mx", "com.ar", "com.sg", "com.hk", "com.tw", "com.cn",
    "co.nz", "co.za", "co.kr", "com.tr", "co.il", "com.pl", "com.es",
}

# Legal and descriptive noise to strip before turning a company name into a slug.
_COMPANY_NOISE = {
    "inc", "inc.", "llc", "l.l.c", "ltd", "ltd.", "limited", "corp", "corp.",
    "corporation", "co", "co.", "company", "gmbh", "ag", "sa", "s.a", "bv", "b.v",
    "nv", "plc", "pty", "pte", "srl", "spa", "oy", "ab", "as", "kk", "kg",
    "holdings", "holding", "group", "technologies", "technology", "tech",
    "solutions", "systems", "software", "labs", "lab", "studios", "studio",
    "partners", "ventures", "capital", "consulting", "services", "international",
    "global", "worldwide", "usa", "us", "the", "and", "&",
}

_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+", re.IGNORECASE)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_SLUG_CLEAN_RE = re.compile(r"[^a-z0-9]+")


def extract_domain(url: str) -> str:
    """Host of a URL, without a leading `www.`. Empty string if there isn't one."""
    try:
        parsed = urlparse(url)
        netloc = (parsed.netloc or "").split("@")[-1].split(":")[0].lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc
    except Exception:
        return ""


def registrable_domain(host: str) -> str:
    """
    `careers.acme.com` -> `acme.com`, `jobs.acme.co.uk` -> `acme.co.uk`.

    Hunter and email patterns work at this level; a subdomain would find nothing.
    """
    host = (host or "").strip().lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    labels = [l for l in host.split(".") if l]
    if len(labels) < 2:
        return host
    if ".".join(labels[-2:]) in MULTIPART_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def is_company_domain(domain: str) -> bool:
    """False for ATS hosts, aggregators, free mail providers, and junk."""
    domain = (domain or "").strip().lower()
    if not domain or "." not in domain or " " in domain:
        return False
    if domain in NON_COMPANY_HOSTS or domain in FREE_EMAIL_DOMAINS:
        return False
    # Also reject subdomains of anything on those lists (acme.applytojob.com).
    parts = domain.split(".")
    for i in range(len(parts) - 1):
        parent = ".".join(parts[i:])
        if parent in NON_COMPANY_HOSTS or parent in FREE_EMAIL_DOMAINS:
            return False
    tld = parts[-1]
    return len(tld) >= 2 and tld.isalpha()


def company_key(name: str) -> str:
    """
    Stable identity for an employer name, so "Acme, Inc." and "Acme Inc" are one
    company. Noise words are dropped only while something is left over.
    """
    tokens = _SLUG_CLEAN_RE.sub(" ", (name or "").lower()).split()
    kept = [t for t in tokens if t not in _COMPANY_NOISE]
    return "".join(kept or tokens)


def domain_candidates_from_name(name: str) -> list[str]:
    """Domains an employer of this name plausibly owns, most likely first."""
    slug = company_key(name)
    if not slug or len(slug) < 2:
        return []
    return [f"{slug}.com", f"{slug}.io", f"{slug}.ai", f"{slug}.co", f"get{slug}.com"]


def domains_in_text(text: str) -> list[str]:
    """Company-looking domains mentioned in free text (links first, then emails)."""
    found: list[str] = []
    for match in _URL_RE.finditer(text or ""):
        domain = registrable_domain(extract_domain(match.group()))
        if is_company_domain(domain) and domain not in found:
            found.append(domain)
    for match in EMAIL_RE.finditer(text or ""):
        domain = registrable_domain(match.group().rsplit("@", 1)[-1])
        if is_company_domain(domain) and domain not in found:
            found.append(domain)
    return found


def domain_responds(domain: str, timeout: float = 6.0) -> bool:
    """Whether a guessed domain is a real site, so we don't query Hunter for noise."""
    for scheme in ("https", "http"):
        try:
            resp = httpx.head(
                f"{scheme}://{domain}", timeout=timeout, follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; jobapp/1.0)"},
            )
            if resp.status_code < 500:
                return True
        except Exception:
            continue
    return False


def resolve_company_domain(
    company: str,
    url: str = "",
    apply_url: str = "",
    description: str = "",
    verify: bool = True,
) -> tuple[str, str]:
    """
    Best guess at the employer's domain, with the evidence that produced it.

    Returns (domain, source) where source is one of "apply_url", "url",
    "description", "name", or "" when nothing looked plausible. Ordering is by
    how much the evidence is worth: an apply link the resolver already followed
    through to the employer beats a name guess by a distance.
    """
    for candidate_url, label in ((apply_url, "apply_url"), (url, "url")):
        domain = registrable_domain(extract_domain(candidate_url or ""))
        if is_company_domain(domain):
            return domain, label

    key = company_key(company)
    text_domains = domains_in_text(description or "")
    if text_domains:
        # A domain whose name echoes the company is far better evidence than the
        # first link in a description, which is often a partner or a CDN.
        for domain in text_domains:
            if key and key in domain.replace(".", "").replace("-", ""):
                return domain, "description"

    for guess in domain_candidates_from_name(company):
        if not verify or domain_responds(guess):
            return guess, "name"

    if text_domains:
        return text_domains[0], "description"

    logger.info("resolve_company_domain: nothing plausible for %r", company)
    return "", ""
