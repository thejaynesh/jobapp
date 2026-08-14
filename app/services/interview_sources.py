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
from urllib.parse import quote

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

# Signs a post is recounting something that happened.
#
# Used to rescue short posts rather than to gate every post, because the search
# already asks for the company and "interview" and the subreddits are about
# interviewing — so demanding another phrase on top mostly discards writeups
# that simply worded it differently. "Just finished my Amazon loop, 4 rounds,
# got the offer" is precisely the post worth keeping and named none of the
# phrases the first version required.
_EXPERIENCE_HINTS = re.compile(
    r"(interview(ed| experience| process)?|onsite|on-site|oa\b|online assessment|"
    r"phone screen|final round|loop\b|\d\s*rounds?\b|got (an|the) offer|"
    r"offer|reject(ed)?|ghosted|hiring manager|recruiter (call|screen|reached))",
    re.I,
)

# Someone asking rather than reporting. A question about an upcoming interview
# is not evidence about the loop, and the corpus is built from evidence.
_ASKING_RE = re.compile(
    r"(what should i|how (do|should) i (prep|prepare|approach)|any (tips|advice)|"
    r"is it worth|should i (apply|accept|take)|does anyone know|can someone (help|explain)|"
    r"looking for (advice|tips|help)|need (help|advice))",
    re.I,
)

# A title that opens like a question usually is one.
_QUESTION_OPENER_RE = re.compile(
    r"^\s*(how|what|should|is|are|can|could|would|does|do|any(one|body)?|help|advice)\b",
    re.I,
)


def is_experience_report(title: str, body: str) -> bool:
    """
    Whether a post recounts an interview rather than asks about one.

    Negative-first, because the search has already narrowed hard: what is left
    to do is discard the questions, not re-prove that a post in r/leetcode
    mentioning a company and "interview" is on topic. A substantial post that
    is not a question is taken at face value; a short one has to say something
    that sounds like an account.
    """
    text = f"{title}\n{body}"
    if _ASKING_RE.search(text):
        return False
    stripped = (title or "").strip()
    if _QUESTION_OPENER_RE.match(stripped) and stripped.endswith("?"):
        return False
    if _EXPERIENCE_HINTS.search(text):
        return True
    # No stated signal, but a long post in an interview subreddit that is not a
    # question is far more often a writeup than not.
    return len(body or "") >= 400

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
    # The server was refused for being a server, not for asking wrongly. A
    # different outcome from an error, because it has a different remedy: ask
    # again from the browser rather than fix the request.
    blocked: bool = False

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

def reddit_search_urls(company: str, limit: int = 25) -> list[str]:
    """
    The search URLs to ask for, whoever ends up asking.

    Separated from fetching because the server is blocked from Reddit and the
    browser is not, so the same URLs get requested from two places.
    """
    if not company.strip():
        return []
    per_sub = max(5, limit // len(_SUBREDDITS))
    query = quote(f'"{company}" interview')
    return [
        f"https://www.reddit.com/r/{sub}/search.json"
        f"?q={query}&restrict_sr=1&sort=new&limit={per_sub}&t=year"
        for sub in _SUBREDDITS
    ]


def parse_reddit(payload, company: str) -> list[dict]:
    """
    Writeups out of one Reddit search response.

    Split from the request so the direct path and the browser path share it —
    the parsing is identical, only the thing that made the request differs.
    """
    reports: list[dict] = []
    if not isinstance(payload, dict):
        return reports

    for child in (payload.get("data") or {}).get("children") or []:
        post = child.get("data") or {}
        title = post.get("title") or ""
        body = post.get("selftext") or ""
        if not is_experience_report(title, body):
            continue
        created = post.get("created_utc")
        if not created:
            continue
        permalink = post.get("permalink") or ""
        try:
            posted_at = datetime.fromtimestamp(float(created), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            continue
        reports.append({
            "source": "reddit",
            "company": company,
            "url": f"https://www.reddit.com{permalink}",
            "title": title,
            "body": body,
            "posted_at": posted_at,
            "role_hint": _role_hint(f"{title} {body[:400]}"),
        })
    return reports


def fetch_reddit(company: str, limit: int = 25, client: httpx.Client | None = None) -> SourceResult:
    """
    Search the interview subreddits, from this server.

    Usually fails in production, and says so precisely. Reddit answers a
    datacenter IP with `403 Blocked` — not a rate limit, a categorical refusal —
    so on a VPS this exists to establish that the browser is needed rather than
    to succeed. `blocked` on the result is what tells the caller to queue the
    same URLs to an agent instead of giving up.
    """
    result = SourceResult(source="reddit")
    if not company.strip():
        return result

    owned = client is None
    client = client or _client()
    try:
        for url in reddit_search_urls(company, limit):
            try:
                response = client.get(url)
                if response.status_code in (403, 429):
                    result.blocked = True
                    result.error = (
                        f"Reddit refused this server ({response.status_code}). "
                        "Datacenter IPs are blocked; queued to your browser instead."
                    )
                    continue
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                result.error = f"{type(exc).__name__}: {exc}"
                continue
            result.reports.extend(parse_reddit(payload, company))
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

        items = payload.get("items") or []
        if not items:
            # Zero results and zero survivors of the filter are different
            # problems with different fixes, and both look like "github: 0".
            result.error = (
                f"search returned no repositories "
                f"(total_count={payload.get('total_count', 'unknown')})"
            )
            return result

        key = normalize_company(company)
        dropped = 0
        for repo in items:
            haystack = f"{repo.get('name','')} {repo.get('description','')}".lower()
            # The search is fuzzy enough to return "interview questions" repos
            # that never mention this company; require the name to appear.
            if key and key not in normalize_company(haystack):
                dropped += 1
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

        if not result.reports and dropped:
            result.error = (
                f"{len(items)} repositories matched the search but none named "
                f"{company!r}"
            )
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
        # Several shapes, because the site has reorganised more than once and
        # the archive is worth the retries. Cheapest and most specific first.
        index_urls = [
            f"https://www.geeksforgeeks.org/tag/{slug}/",
            f"https://www.geeksforgeeks.org/tag/{slug}-interview-experience/",
            f"https://www.geeksforgeeks.org/category/interview-experiences/{slug}/",
            f"https://www.geeksforgeeks.org/company/{slug}/",
            f"https://www.geeksforgeeks.org/?s={slug}+interview+experience",
        ]
        tried: list[str] = []
        links: list[str] = []
        for index_url in index_urls:
            tried.append(index_url)
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
            # Nothing matched. Naming what was tried, because "the archive has
            # nothing on this company" and "the index moved again" are the same
            # silence otherwise, and only one of them is fixable here.
            result.error = (
                "no interview-experience links found. Tried: "
                + ", ".join(u.replace("https://www.geeksforgeeks.org", "") for u in tried)
            )

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
            diagnostics[name] = {
                "count": 0, "error": f"{type(exc).__name__}: {exc}", "blocked": False,
            }
            continue
        reports.extend(outcome.reports)
        diagnostics[name] = {
            "count": outcome.count,
            "error": outcome.error,
            "blocked": outcome.blocked,
        }

    return {"reports": reports, "sources": diagnostics}
