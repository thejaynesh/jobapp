"""
Google's job results — the panel a Google search for "<role> jobs" opens —
through SerpApi.

Google for Jobs is the widest aggregator there is: it indexes the JobPosting
data nearly every board and careers site publishes, so one search returns
postings from LinkedIn, Indeed, the company's own site and a dozen boards this
application has no adapter for. It has no API of its own and answers a server
with a challenge, so it is read through SerpApi, which runs the search and
returns the panel as JSON. That needs `SERPAPI_API_KEY`.

Each page is one search against a monthly quota — 250 on SerpApi's free plan
as of writing — so the budget is the design: a cap on searches per run, and a
minimum interval between runs (`google_jobs_interval_hours`) enforced by the
fetcher, since the API group itself runs every couple of hours.

JSearch (RapidAPI) reads the same index by another route; the two overlap, and
dedupe merges what they share.
"""

import logging
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from app.services.sources.base import (
    SourceUnavailable,
    _employment_type,
    in_united_states,
    parse_experience_level,
    posted_at_from_age,
    raise_if_blocked,
)

logger = logging.getLogger(__name__)

_ENDPOINT = "https://serpapi.com/search.json"

DEFAULT_MAX_SEARCHES = 8
DEFAULT_PAGES = 1

_REMOTE_WORDS = {"remote", "anywhere", "worldwide", "work from home"}

# "16.25–18.44 an hour", "120K–150K a year", "$95K a year", "Up to 60 an hour".
_PAY = re.compile(
    r"(?P<up_to>up to\s+)?(?P<sym>[$£€])?\s*(?P<low>\d[\d,]*(?:\.\d+)?)\s*(?P<low_k>[kK])?"
    r"(?:\s*[–—-]\s*[$£€]?\s*(?P<high>\d[\d,]*(?:\.\d+)?)\s*(?P<high_k>[kK])?)?"
    r"\s+(?:an?|per)\s+(?P<period>hour|day|week|month|year)",
    re.I,
)
_CURRENCIES = {"$": "USD", "£": "GBP", "€": "EUR"}


def _query_text(role: str, location: str) -> str:
    """
    The search as a person would type it.

    SerpApi's own `location` parameter only accepts places from Google's
    canonical list and refuses anything else ("Unsupported location"), and a
    profile's locations are free text — "Remote", "NYC", "Bay Area". Google's
    job search reads a place in the query itself, so it goes there.
    """
    role, location = role.strip(), (location or "").strip()
    if not location:
        return role
    if location.lower() in _REMOTE_WORDS:
        return f"{role} remote"
    return f"{role} in {location}"


def _pay(text: str, location: str) -> dict:
    """
    A pay band out of Google's display string, or {} when it is not safe to keep.

    Google writes the figures without a currency for local postings. A band
    stored without one is worse than none: `enrich_from` takes a band only as
    a whole, so it would stop the posting page's own (which usually does name
    it) from ever landing. So the currency must be printed, or the posting
    plainly in the US.
    """
    match = _PAY.search(text or "")
    if not match:
        return {}
    currency = _CURRENCIES.get(match.group("sym") or "")
    if not currency and in_united_states(location):
        currency = "USD"
    if not currency:
        return {}

    def amount(digits, k):
        return float(digits.replace(",", "")) * (1000 if k else 1)

    low = amount(match.group("low"), match.group("low_k"))
    high = amount(match.group("high"), match.group("high_k")) if match.group("high") else None
    if match.group("up_to") and high is None:
        # "Up to 60 an hour" is a ceiling. Filed as the floor it would admit
        # the posting to a filter asking for at least 60.
        low, high = None, low
    return {"salary_min": low, "salary_max": high, "salary_currency": currency,
            "salary_period": match.group("period").lower()}


def _strip_tracking(url: str) -> str:
    """The link without `utm_*`, so the same posting has the same address."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_")]
    return urlunsplit(parts._replace(query=urlencode(query)))


def _best_link(item: dict) -> str:
    """
    Which of the posting's apply links to keep as its address.

    Google lists every place it found the posting — the employer's careers
    page, its ATS, LinkedIn, Indeed, a dozen reposting sites. The employer's own
    comes first here: it is where a person should apply, and an ATS address is
    what board discovery turns into a whole company's board on every later
    cycle. An aggregator's copy is the fallback, and Google's own share link
    the last resort.
    """
    from app.services.enrichment import looks_like_ats
    from app.services.link_resolver import is_aggregator

    company = (item.get("company_name") or "").strip().lower()
    options = [o for o in (item.get("apply_options") or [])
               if isinstance(o, dict) and str(o.get("link") or "").startswith("http")]

    def rank(option):
        link = option["link"]
        own = bool(company) and company in str(option.get("title") or "").lower()
        if looks_like_ats(link) or own:
            return 0
        return 2 if is_aggregator(link) else 1

    if options:
        return _strip_tracking(min(options, key=rank)["link"])
    return str(item.get("share_link") or "")


def _as_job(item: dict) -> dict | None:
    title = (item.get("title") or "").strip()
    company = (item.get("company_name") or "").strip()
    url = _best_link(item)
    if not title or not company or not url:
        return None
    extras = item.get("detected_extensions") or {}
    location = (item.get("location") or "").strip()
    description = item.get("description") or ""
    if not description:
        # Qualifications and responsibilities, when Google kept no prose.
        description = "\n".join(
            str(line)
            for block in (item.get("job_highlights") or []) if isinstance(block, dict)
            for line in (block.get("items") or [])
        )
    remote = bool(extras.get("work_from_home")) or any(
        word in location.lower() for word in _REMOTE_WORDS)
    return {
        "source": "google_jobs",
        "source_job_id": item.get("job_id") or None,
        "title": title,
        "company": company,
        "location": location,
        "is_remote": remote,
        "url": url,
        "description": description,
        "experience_level": parse_experience_level(title, description),
        "posted_at": posted_at_from_age(extras.get("posted_at")),
        "employment_type": _employment_type(extras.get("schedule_type")),
        **_pay(extras.get("salary") or "", location),
    }


def _redacted(exc: Exception, api_key: str) -> str:
    """
    An error message without the key in it.

    SerpApi takes its key as a query parameter, and httpx names the URL in its
    errors — so an unredacted message would carry the key into the logs, into
    the stored fetch history, and onto the Runs page that displays it.
    """
    text = str(exc)
    return text.replace(api_key, "***") if api_key else text


def _search(api_key: str, text: str, token: str | None) -> dict:
    """
    One search (one page). Raises SourceUnavailable when the key or the quota
    is the problem; returns {} for anything that only this search suffered.
    """
    params = {"engine": "google_jobs", "q": text, "hl": "en", "api_key": api_key}
    if token:
        params["next_page_token"] = token
    resp = httpx.get(_ENDPOINT, params=params, timeout=30)
    raise_if_blocked(resp, "SerpApi (Google Jobs)")
    try:
        data = resp.json()
    except ValueError:
        resp.raise_for_status()
        raise
    error = str((data or {}).get("error") or "")
    if error:
        # Google finding nothing arrives as an "error" too, and is not one.
        if "hasn't returned any results" in error:
            return {}
        if re.search(r"api key|run out of searches|plan|account", error, re.I):
            raise SourceUnavailable(f"SerpApi: {error}")
        logger.error("Google Jobs search for %r failed: %s", text, error)
        return {}
    resp.raise_for_status()
    return data


def fetch_all(api_key: str, queries: list[str], locations: list[str],
              max_searches: int = DEFAULT_MAX_SEARCHES,
              pages: int = DEFAULT_PAGES) -> list[dict]:
    """
    Every role in every location, within `max_searches` searches in all.

    First pages for every search before any second page, so a small budget
    buys breadth — ten roles once each — rather than one role ten deep. A
    search that stops being able to answer (key refused, quota spent) keeps
    what the earlier ones found, and is logged as the error it is.
    """
    searches = [(q, loc) for q in queries for loc in (locations or [""])]
    budget = max(0, int(max_searches))
    tokens: dict[tuple[str, str], str | None] = {}
    jobs: dict[str, dict] = {}
    used = 0
    cut_short = False
    try:
        for depth in range(max(1, int(pages))):
            pending = searches if depth == 0 else [s for s in searches if tokens.get(s)]
            for search in pending:
                if used >= budget:
                    cut_short = True
                    break
                used += 1
                try:
                    data = _search(api_key, _query_text(*search), tokens.get(search))
                except (httpx.HTTPError, ValueError) as exc:
                    logger.error("Google Jobs search for %r failed: %s",
                                 _query_text(*search), _redacted(exc, api_key))
                    tokens[search] = None
                    continue
                tokens[search] = ((data.get("serpapi_pagination") or {})
                                  .get("next_page_token"))
                for item in data.get("jobs_results") or []:
                    job = _as_job(item) if isinstance(item, dict) else None
                    if job:
                        jobs.setdefault(job["source_job_id"] or job["url"], job)
    except SourceUnavailable as exc:
        logger.error("%s (after %d search(es); keeping what they found)",
                     _redacted(exc, api_key), used)

    if cut_short:
        logger.warning(
            "Google Jobs: stopped at the %d-search budget with %d role/location "
            "search(es) configured; raise it on the settings page to cover more",
            budget, len(searches),
        )
    logger.info("Google Jobs: %d jobs from %d search(es)", len(jobs), used)
    return list(jobs.values())
