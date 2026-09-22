import logging
import re

from app.services.sources.base import parse_experience_level
from app.services.sources.playwright_base import (
    CONTEXT_OPTIONS,
    LAUNCH_OPTIONS,
    describe_page,
    encode,
    is_remote_location,
)

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.dice.com"

# https://www.dice.com/job-detail/<guid> — the id is the last path segment.
_JOB_DETAIL_RE = re.compile(r"/job-detail/([^/?#]+)")


def _job_id_from_url(url: str) -> str | None:
    match = _JOB_DETAIL_RE.search(url or "")
    return match.group(1) if match else None

# The old extractor keyed entirely on <dhi-job-card>, an Angular custom element
# Dice no longer renders — the page loads fine (real title, full body) but that
# selector never appears, so every search timed out with nothing.
#
# Rather than swap in today's class names and be broken again at the next
# redesign, extraction is layered from most durable to least:
#   1. JSON-LD JobPosting — structured data Dice publishes for search engines,
#      independent of markup entirely.
#   2. Embedded app state (__NEXT_DATA__ etc.).
#   3. Anchors pointing at /job-detail/, which is a stable URL shape whatever
#      the surrounding card looks like.
_READY_SELECTOR = (
    'a[href*="/job-detail/"], dhi-job-card, [data-cy="card-title-link"], '
    'script[type="application/ld+json"]'
)

_EXTRACT_JS = """() => {
    const out = [];
    const seen = new Set();
    const push = (job) => {
        if (!job || !job.title || !job.url || seen.has(job.url)) return;
        seen.add(job.url);
        out.push(job);
    };
    const abs = (href) => {
        if (!href) return '';
        return href.startsWith('http') ? href : ('https://www.dice.com' + href);
    };

    // 1. JSON-LD structured data.
    for (const el of document.querySelectorAll('script[type="application/ld+json"]')) {
        let data;
        try { data = JSON.parse(el.textContent); } catch (e) { continue; }
        const items = Array.isArray(data) ? data : [data];
        for (const item of items) {
            const graph = item['@graph'] || [item];
            for (const node of graph) {
                if (!node || node['@type'] !== 'JobPosting') continue;
                const org = node.hiringOrganization || {};
                const loc = node.jobLocation || {};
                const addr = (Array.isArray(loc) ? (loc[0] || {}) : loc).address || {};
                push({
                    title: (node.title || '').trim(),
                    company: (typeof org === 'string' ? org : org.name || '').trim(),
                    location: [addr.addressLocality, addr.addressRegion]
                        .filter(Boolean).join(', '),
                    url: abs(node.url || ''),
                    // Left as markup: the server cleans descriptions in one
                    // place, and a regex here would strip the list structure
                    // out before it ever got there.
                    description: node.description || '',
                    postedAt: node.datePosted || '',
                    employmentType: Array.isArray(node.employmentType)
                        ? node.employmentType[0] : (node.employmentType || ''),
                });
            }
        }
    }
    if (out.length) return out;

    // 2. Embedded app state.
    for (const el of document.querySelectorAll('script[id="__NEXT_DATA__"]')) {
        let data;
        try { data = JSON.parse(el.textContent); } catch (e) { continue; }
        const stack = [data];
        while (stack.length) {
            const node = stack.pop();
            if (!node || typeof node !== 'object') continue;
            if (Array.isArray(node)) { stack.push(...node); continue; }
            const title = node.title || node.jobTitle;
            const id = node.id || node.jobId || node.guid;
            if (title && id && (node.companyName || node.company)) {
                const company = node.companyName || node.company;
                push({
                    title: String(title).trim(),
                    company: String(typeof company === 'object'
                        ? (company.name || '') : company).trim(),
                    location: String(node.jobLocation || node.location ||
                                     node.formattedLocation || '').trim(),
                    url: abs('/job-detail/' + id),
                    description: String(node.summary || node.description || ''),
                    postedAt: String(node.postedDate || node.datePosted ||
                                     node.modifiedDate || ''),
                });
            }
            for (const v of Object.values(node)) {
                if (v && typeof v === 'object') stack.push(v);
            }
        }
    }
    if (out.length) return out;

    // 3. Job-detail links, whatever the card markup is.
    for (const a of document.querySelectorAll('a[href*="/job-detail/"]')) {
        const title = (a.innerText || '').trim();
        if (!title || title.length < 3) continue;
        // Walk up to whatever wraps the link and read its text for context.
        let card = a;
        for (let i = 0; i < 4 && card.parentElement; i++) card = card.parentElement;
        const text = (card.innerText || '').split('\\n')
            .map(s => s.trim()).filter(Boolean);
        const idx = text.indexOf(title);
        push({
            title: title,
            company: idx >= 0 && text[idx + 1] ? text[idx + 1] : '',
            location: idx >= 0 && text[idx + 2] ? text[idx + 2] : '',
            url: abs(a.getAttribute('href')),
            description: '',
        });
    }
    return out;
}"""


async def _scrape(query: str, location: str) -> list[dict]:
    from playwright.async_api import async_playwright

    url = (
        f"{_BASE_URL}/jobs?q={encode(query)}&location={encode(location)}"
        f"&countryCode=US&radius=30&radiusUnit=mi&pageSize=20&language=en"
    )
    async with async_playwright() as p:
        browser = await p.chromium.launch(**LAUNCH_OPTIONS)
        context = await browser.new_context(**CONTEXT_OPTIONS)
        page = await context.new_page()
        try:
            await page.goto(url, timeout=30000, wait_until="domcontentloaded")
        except Exception as exc:
            logger.warning("Dice: page load failed (%s) — %s",
                           type(exc).__name__, await describe_page(page))
            await browser.close()
            return []

        # A missing selector is no longer fatal: wait for one if it turns up,
        # then extract regardless — JSON-LD is often present before any card is.
        try:
            await page.wait_for_selector(_READY_SELECTOR, timeout=12000)
        except Exception:
            logger.info("Dice: no card selector matched; trying structured data anyway")

        try:
            job_data = await page.evaluate(_EXTRACT_JS)
        except Exception as exc:
            logger.warning("Dice: extraction failed (%s) — %s",
                           type(exc).__name__, await describe_page(page))
            await browser.close()
            return []

        if not job_data:
            logger.warning("Dice: no jobs found by any extraction method — %s",
                           await describe_page(page))
        await browser.close()

        jobs = []
        for d in job_data:
            title = (d.get("title") or "").strip()
            if not title:
                continue
            loc = (d.get("location") or "").strip()
            desc = (d.get("description") or "").strip()
            url = d.get("url") or ""
            jobs.append({
                "source": "dice",
                # The id is right there in the URL Dice already gave us.
                # Leaving it None threw away the strongest dedupe key the
                # source has, so the same posting re-inserted itself under
                # every cosmetic title change.
                "source_job_id": _job_id_from_url(url),
                "title": title,
                "company": (d.get("company") or "").strip(),
                "location": loc,
                "is_remote": is_remote_location(loc, title),
                "url": url,
                "description": desc,
                "experience_level": parse_experience_level(title, desc),
                # Dice publishes datePosted in its structured data; not reading
                # it is why 97% of stored Dice jobs have no date, which in turn
                # is why the staleness filter can never drop an old one.
                "posted_at": (d.get("postedAt") or "").strip() or None,
            })
        with_desc = sum(1 for j in jobs if j["description"])
        logger.info(
            "Dice: %d jobs for %s / %s (%d with a description, %d dated)",
            len(jobs), query, location, with_desc,
            sum(1 for j in jobs if j["posted_at"]),
        )
        if jobs and not with_desc:
            # Not fatal any more: the search page has never carried
            # descriptions, and enrichment fetches them from the job-detail
            # URLs afterwards. Said out loud so the panel's "0 chars" reads as
            # expected rather than as a broken adapter.
            logger.info(
                "Dice: search results carry no descriptions; enrichment will "
                "fetch them from the job-detail pages"
            )
        return jobs


async def fetch(query: str, location: str) -> list[dict]:
    """The browser scrape. Only the fallback now — see `fetch_api`."""
    try:
        return await _scrape(query, location)
    except Exception as exc:
        logger.error("Dice fetch error: %s", exc)
        return []


# ---------------------------------------------------------------------------
# The search API
# ---------------------------------------------------------------------------
#
# Dice's own search page is a client of a JSON API, and that API answers a
# plain HTTP request with structured results: title, employer, location,
# posting date, stated pay, remote flag, sponsorship, and a summary. The
# browser scrape it replaces launched Chromium to read cards that carried none
# of that — and was the reason Dice sat in the expensive tier at all.
#
# The key is not ours: it is the public one Dice's own front end sends, which
# is why it is a setting (`DICE_API_KEY`) rather than a constant — if Dice
# rotates it, the new one is in the request headers of any dice.com search and
# changing it is not a deploy. A rejected key is reported as such, and the
# caller falls back to the browser scrape rather than going dark.

_API = "https://job-search-api.svc.dhigroupinc.com/v1/dice/jobs/search"
_API_PAGE_SIZE = 50
_SALARY_RE = re.compile(
    r"(?P<cur>[A-Z]{3})?\s*\$?(?P<lo>[\d,]+(?:\.\d+)?)"
    r"(?:\s*[-–]\s*\$?(?P<hi>[\d,]+(?:\.\d+)?))?\s*(?:per\s+(?P<per>\w+))?",
    re.I,
)


class DiceApiUnavailable(Exception):
    """The API refused or failed; the browser scrape is the fallback."""


def _salary(text: str | None) -> dict:
    """"USD 73,840.00 - 94,667.00 per year" as the columns the fetcher stores."""
    if not text:
        return {}
    match = _SALARY_RE.search(text)
    if not match:
        return {}
    try:
        low = float(match.group("lo").replace(",", ""))
        high = float((match.group("hi") or match.group("lo")).replace(",", ""))
    except (TypeError, ValueError):
        return {}
    if low <= 0:
        return {}
    out = {"salary_min": low, "salary_max": max(low, high)}
    if match.group("cur"):
        out["salary_currency"] = match.group("cur").upper()
    elif "$" in text:
        out["salary_currency"] = "USD"
    if match.group("per"):
        out["salary_period"] = match.group("per").lower()
    return out


def _from_api(item: dict) -> dict | None:
    title = (item.get("title") or "").strip()
    url = item.get("detailsPageUrl") or ""
    if not title or not url:
        return None
    place = item.get("jobLocation") or {}
    location = (place.get("displayName") or "").strip() if isinstance(place, dict) else ""
    remote = bool(item.get("isRemote")) or str(
        item.get("workFromHomeAvailability") or "").upper() == "TRUE"
    summary = (item.get("summary") or "").strip()
    job = {
        "source": "dice",
        # The detail page's guid, which is what the scrape recorded too — so a
        # posting stored before this change is recognised as the same one.
        "source_job_id": _job_id_from_url(url) or item.get("guid") or item.get("id"),
        "title": title,
        "company": (item.get("companyName") or "").strip(),
        "location": location or ("Remote" if remote else ""),
        "is_remote": remote or is_remote_location(location, title),
        "url": url,
        # A summary is the first few hundred characters, not the posting.
        # Stored so matching has something; enrichment fetches the whole page.
        "description": summary,
        "experience_level": parse_experience_level(title, summary),
        "posted_at": item.get("firstActiveDate") or item.get("postedDate"),
        "employment_type": (item.get("employmentType") or "").strip() or None,
        **_salary(item.get("salary")),
    }
    return job


def fetch_api(query: str, location: str = "", max_pages: int = 2,
              api_key: str | None = None, posted_within: str = "SEVEN") -> list[dict]:
    """
    Search Dice's JSON API. Raises `DiceApiUnavailable` when it cannot answer.

    Recent postings first by the API's own filter (`SEVEN` days), paged up to
    `max_pages` of fifty. An empty location searches the whole US.
    """
    import httpx

    from app.config import settings

    key = api_key or getattr(settings, "DICE_API_KEY", "")
    if not key:
        raise DiceApiUnavailable("no DICE_API_KEY configured")
    headers = {"x-api-key": key, "Accept": "application/json",
               "User-Agent": "Mozilla/5.0 (compatible; jobapp)"}
    params = {
        "q": query, "countryCode2": "US", "radius": 30, "radiusUnit": "mi",
        "pageSize": _API_PAGE_SIZE, "language": "en",
        "filters.postedDate": posted_within,
    }
    remote_search = (location or "").strip().lower() == "remote"
    if remote_search:
        # The workplace filter is what Dice's own "Remote" chip sends, and it
        # does narrow the results — but the rows it returns carry no remote
        # flag of their own (`workFromHomeAvailability` is a legacy field that
        # reads FALSE on remote postings), so the search is the evidence.
        params["filters.workplaceTypes"] = "Remote"
    elif location and location.strip().lower() not in ("united states", "usa", "us"):
        params["location"] = location

    jobs: list[dict] = []
    seen: set[str] = set()
    for page in range(1, max(1, max_pages) + 1):
        try:
            resp = httpx.get(_API, params={**params, "page": page}, headers=headers,
                             timeout=20)
        except Exception as exc:
            if page == 1:
                raise DiceApiUnavailable(f"request failed: {exc}") from exc
            break
        if resp.status_code in (401, 403):
            raise DiceApiUnavailable(
                f"the API refused the key ({resp.status_code}); copy the current "
                "x-api-key from a dice.com search into DICE_API_KEY"
            )
        if resp.status_code >= 400:
            if page == 1:
                raise DiceApiUnavailable(f"HTTP {resp.status_code}")
            break
        try:
            data = resp.json()
        except ValueError as exc:
            raise DiceApiUnavailable("the API answered with something other than JSON") from exc
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list) or not rows:
            break
        for item in rows:
            job = _from_api(item) if isinstance(item, dict) else None
            if job and remote_search:
                job["is_remote"] = True
            if job and job["url"] not in seen:
                seen.add(job["url"])
                jobs.append(job)
        meta = data.get("meta") or {}
        if page >= int(meta.get("pageCount") or page):
            break
    logger.info("Dice API: %d jobs for %s / %s", len(jobs), query, location or "US")
    return jobs
