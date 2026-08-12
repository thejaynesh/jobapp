"""
Where interview writeups come from, for the sources that need no credentials.

Three, in ascending order of how much they can be trusted to keep working.

**Reddit** is the most dependable. Appending `.json` to any listing URL returns
the same structure the site itself renders from, it needs no key, and the shape
(`data.children[].data`) has been stable for over a decade. Dates arrive as
`created_utc`, which is exactly the field the corpus refuses to do without.

**GitHub** is next. A documented, versioned API, and the token already
configured for contact discovery lifts the rate limit. What it holds is curated
question collections rather than personal writeups, so it fills a different gap:
what gets asked, rather than what one loop felt like.

**GeeksforGeeks** has the largest archive already organised by company, and is
plain HTML with no auth — but it is HTML, which means selectors, which means it
breaks on a redesign and breaks *quietly*. That is handled two ways: parsing is
shape-tolerant rather than tied to one class name, and every fetch reports its
yield so a source that has started returning nothing is visible instead of
merely silent.

None of this is verifiable from here — the build environment cannot reach any of
these hosts, so the request shapes are written from documentation and the HTML
parsing is defensive by necessity rather than by preference. `fetch_all` returns
per-source counts and errors for that reason; treat a zero from a source that
previously yielded as a bug report rather than as an absence of data.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from app.config import settings
from app.services.company_domain import company_key as normalize_company

logger = logging.getLogger(__name__)

_UA = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

DEFAULT_TIMEOUT = 20

# Subreddits where interview writeups actually appear.
_SUBREDDITS = ("leetcode", "cscareerquestions", "csMajors", "ExperiencedDevs")

# Phrases that mark a post as an experience report rather than a question.
_EXPERIENCE_HINTS = re.compile(
    r"(interview experience|interview process|onsite|oa |online assessment|"
    r"phone screen|final round|got (an )?offer|rejected after)",
    re.I,
)

# Level hints as people actually write them in titles.
_ROLE_HINT_RE = re.compile(
    r"\b(intern(?:ship)?|new ?grad|university|campus|sde[- ]?[123]|l[3-7]\b|"
    r"e[3-7]\b|senior|staff|principal|entry[- ]level|fresher)\b",
    re.I,
)

# A GfG interview-experience article title almost always names the company and
# says what it is: "Amazon Interview Experience for SDE-1 (On-Campus)".
_GFG_TITLE_RE = re.compile(r"interview experience", re.I)


@dataclass
class SourceResult:
    """One source's contribution, with enough detail to notice a break."""

    source: str
    reports: list[dict] = field(default_factory=list)
    error: str | None = None

    @property
    def count(self) -> int:
        return len(self.reports)


def _client(timeout: int = DEFAULT_TIMEOUT) -> httpx.Client:
    return httpx.Client(headers=_UA, timeout=timeout, follow_redirects=True)


def _role_hint(text: str) -> str | None:
    match = _ROLE_HINT_RE.search(text or "")
    return match.group(0) if match else None


# ---------------------------------------------------------------------------
# Reddit
# ---------------------------------------------------------------------------

def fetch_reddit(company: str, limit: int = 25, client: httpx.Client | None = None) -> SourceResult:
    """
    Search the interview subreddits for writeups about this company.

    `.json` on a search URL returns what the site renders from — no key, no
    scraping, and `created_utc` on every post, which is the field that decides
    whether a report is usable at all.
    """
    result = SourceResult(source="reddit")
    if not company.strip():
        return result

    owned = client is None
    client = client or _client()
    try:
        for subreddit in _SUBREDDITS:
            url = f"https://www.reddit.com/r/{subreddit}/search.json"
            params = {
                "q": f'"{company}" interview',
                "restrict_sr": "1",
                "sort": "new",
                "limit": str(max(5, limit // len(_SUBREDDITS))),
                "t": "year",
            }
            try:
                response = client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                result.error = f"{type(exc).__name__}: {exc}"
                continue

            for child in (payload.get("data") or {}).get("children") or []:
                post = child.get("data") or {}
                title = post.get("title") or ""
                body = post.get("selftext") or ""
                if not _EXPERIENCE_HINTS.search(f"{title}\n{body}"):
                    continue
                created = post.get("created_utc")
                if not created:
                    continue
                permalink = post.get("permalink") or ""
                result.reports.append({
                    "source": "reddit",
                    "company": company,
                    "url": f"https://www.reddit.com{permalink}",
                    "title": title,
                    "body": body,
                    "posted_at": datetime.fromtimestamp(float(created), tz=timezone.utc),
                    "role_hint": _role_hint(f"{title} {body[:400]}"),
                })
    finally:
        if owned:
            client.close()
    return result


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

def fetch_github(company: str, limit: int = 10, client: httpx.Client | None = None) -> SourceResult:
    """
    Curated question collections mentioning this company.

    Different in kind from a personal writeup: what gets asked rather than what
    one loop felt like. `pushed_at` dates the collection, which is the closest
    thing to a report date these have — and it is a real signal, since an
    unmaintained list describes an old loop.
    """
    result = SourceResult(source="github")
    if not company.strip():
        return result

    headers = dict(_UA)
    token = (getattr(settings, "GITHUB_TOKEN", "") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    owned = client is None
    client = client or _client()
    try:
        try:
            response = client.get(
                "https://api.github.com/search/repositories",
                params={
                    "q": f"{company} interview questions in:name,description,readme",
                    "sort": "updated",
                    "per_page": str(limit),
                },
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            return result

        key = normalize_company(company)
        for repo in payload.get("items") or []:
            haystack = f"{repo.get('name','')} {repo.get('description','')}".lower()
            # The search is fuzzy enough to return "interview questions" repos
            # that never mention this company; require the name to appear.
            if key and key not in normalize_company(haystack):
                continue
            pushed = repo.get("pushed_at") or repo.get("updated_at")
            if not pushed:
                continue
            try:
                posted_at = datetime.fromisoformat(pushed.replace("Z", "+00:00"))
            except ValueError:
                continue
            result.reports.append({
                "source": "github",
                "company": company,
                "url": repo.get("html_url") or "",
                "title": repo.get("full_name") or "",
                "body": repo.get("description") or "",
                "posted_at": posted_at,
                "role_hint": None,
            })
    finally:
        if owned:
            client.close()
    return result


# ---------------------------------------------------------------------------
# GeeksforGeeks
# ---------------------------------------------------------------------------

_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")
_TITLE_TAG_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_DATE_META_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:article:published_time|datePublished)["\']'
    r'[^>]+content=["\']([^"\']+)',
    re.I,
)
_DATE_JSON_RE = re.compile(r'"datePublished"\s*:\s*"([^"]+)"', re.I)


def _text_of(html: str) -> str:
    return re.sub(r"\s+", " ", _TAG_STRIP_RE.sub(" ", html)).strip()


def _published_at(html: str) -> datetime | None:
    """
    The publication date, from metadata rather than from the rendered page.

    Both patterns are structured data the site emits for search engines, which
    is markedly more stable than whatever element currently displays the date —
    and the corpus refuses undated reports, so this is the load-bearing part of
    the parse.
    """
    for pattern in (_DATE_META_RE, _DATE_JSON_RE):
        match = pattern.search(html)
        if not match:
            continue
        raw = match.group(1).strip()
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def parse_gfg_article(html: str, url: str, company: str) -> dict | None:
    """
    One writeup out of an article page.

    Separated from fetching so it can be tested against saved HTML, which is the
    only way this gets exercised at all: the build environment cannot reach the
    site, so every selector here is written from documentation and inference
    rather than from a page anybody looked at.
    """
    posted_at = _published_at(html)
    if not posted_at:
        return None

    title_match = _TITLE_TAG_RE.search(html)
    title = _text_of(title_match.group(1)) if title_match else ""

    body = _text_of(html)
    if len(body) < 200:
        return None

    return {
        "source": "geeksforgeeks",
        "company": company,
        "url": url,
        "title": title[:500],
        "body": body[:60000],
        "posted_at": posted_at,
        "role_hint": _role_hint(title),
    }


def fetch_geeksforgeeks(
    company: str, limit: int = 10, client: httpx.Client | None = None
) -> SourceResult:
    """
    Interview-experience articles for a company.

    The archive is organised by company tag, which is why this source is worth
    the fragility: it is the largest per-company collection available without a
    login. Link discovery deliberately matches on URL and anchor text rather
    than on page structure, so a redesign that moves the list around does not
    necessarily break it.
    """
    result = SourceResult(source="geeksforgeeks")
    slug = normalize_company(company)
    if not slug:
        return result

    owned = client is None
    client = client or _client()
    try:
        index_urls = [
            f"https://www.geeksforgeeks.org/tag/{slug}-interview-experience/",
            f"https://www.geeksforgeeks.org/category/interview-experiences/{slug}/",
        ]
        links: list[str] = []
        for index_url in index_urls:
            try:
                response = client.get(index_url)
                if response.status_code != 200:
                    continue
                for href in _HREF_RE.findall(response.text):
                    if "geeksforgeeks.org" not in href or href in links:
                        continue
                    if not _GFG_TITLE_RE.search(href.replace("-", " ")):
                        continue
                    links.append(href)
            except Exception as exc:
                result.error = f"{type(exc).__name__}: {exc}"
            if links:
                break

        if not links and not result.error:
            # Nothing matched. Said explicitly, because a company with no
            # coverage and a parser that has stopped working are the same
            # silence otherwise.
            result.error = "no interview-experience links found on the index pages"

        for href in links[:limit]:
            try:
                article = client.get(href)
                if article.status_code != 200:
                    continue
                parsed = parse_gfg_article(article.text, href, company)
                if parsed:
                    result.reports.append(parsed)
            except Exception as exc:
                logger.debug("geeksforgeeks: %s failed: %s", href, exc)
    finally:
        if owned:
            client.close()
    return result


# ---------------------------------------------------------------------------
# All of them
# ---------------------------------------------------------------------------

FETCHERS = {
    "reddit": fetch_reddit,
    "github": fetch_github,
    "geeksforgeeks": fetch_geeksforgeeks,
}


def fetch_all(company: str, only: set[str] | None = None) -> dict:
    """
    Every free source, for one company.

    One source failing never stops the others: they are independent, and a
    corpus built from two of three is worth more than an exception. Per-source
    counts and errors come back so a break is legible rather than inferred from
    a total that looks lower than usual.
    """
    reports: list[dict] = []
    diagnostics: dict[str, dict] = {}

    for name, fetcher in FETCHERS.items():
        if only and name not in only:
            continue
        try:
            outcome = fetcher(company)
        except Exception as exc:
            logger.error("interview_sources: %s raised for %s: %s", name, company, exc)
            diagnostics[name] = {"count": 0, "error": f"{type(exc).__name__}: {exc}"}
            continue
        reports.extend(outcome.reports)
        diagnostics[name] = {"count": outcome.count, "error": outcome.error}

    return {"reports": reports, "sources": diagnostics}
