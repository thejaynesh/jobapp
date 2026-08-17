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
    try:
        return await _scrape(query, location)
    except Exception as exc:
        logger.error("Dice fetch error: %s", exc)
        return []
