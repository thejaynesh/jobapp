"""
Avature career portals: `<tenant>.avature.net/<portal>`, often shown under
the employer's own domain (`jobsearch.harman.com`, `jobs.pomerleau.ca`).

Avature carries large employers the other readers miss — Bloomberg, Koch and
its companies (Molex, Guardian, Georgia-Pacific), Harman, Delta, IBM, Two
Sigma — and nine hosts in SimplifyJobs' lists. It has no public API, but each
portal publishes a sitemap of every open posting, named in the tenant's
`robots.txt`, and each entry's URL carries the posting's title and id:

    Sitemap: https://bloomberg.avature.net/careers/sitemap_index.xml
      → https://bloomberg.avature.net/careers/sitemap.xml
        <loc>…/careers/JobDetail/Technical-Product-Manager-SDLC-Tools/22344</loc>
        <lastmod>2026-09-24</lastmod>

The sitemap is the whole board: Bloomberg's 348 entries are the 348 its search
reports, and Koch's 2,510 are the same in each of its three languages
(measured 2026-09-28). So a posting it stops listing has closed
(`job_fetcher.FULL_FEED_BOARDS`), and the search pages — twelve postings each,
six at Siemens — are never needed.

From the sitemap alone, the titles are matched against the profile's roles,
and only those postings' pages are read, once each (the ones already stored
with their text are not read again). A page is labelled fields — Location,
Company, Job Number — and the unlabelled text is the description. A few
portals put a number where the title slug should be (Pomerleau's
`JobDetail/7190/3915`); those are read to learn the title, a capped number a
cycle, and not read again for a week.

The date is the sitemap's `lastmod`, unless the page states a posted date.
It moves when a posting is edited, so an old posting someone touched looks
newer than it is; it never makes a new one look old.

What stays unread, on purpose:

* Portals whose robots.txt names no sitemap for them. The sitemap is how a
  portal says it is public.
* Internal-mobility portals (`internalcareers`): their pages redirect to a
  login, which reads as no posting.
* Tenants behind an AWS bot challenge (IBM, Delta, ManTech: HTTP 202 with
  `x-amzn-waf-action: challenge`). Those are refusals, and they stay refused.

A board is `host/portal`, the host being the tenant's avature.net name:
`bloomberg.avature.net/careers`, `koch.avature.net/CollegeRecruiting`.
"""

import logging
import re
import time
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from urllib.parse import unquote, urlparse

import httpx

from app.services.descriptions import clean as clean_description
from app.services.sources.base import (
    age_cutoff,
    board_workers,
    cycle_cfg,
    described,
    fetch_boards_concurrently,
    parse_experience_level,
    saw_postings,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_DETAILS = 25
_TIMEOUT = 30
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
}

_SPEC = re.compile(r"^([a-z0-9-]+(?:\.[a-z0-9-]+)+)/([A-Za-z0-9_-]+)$", re.I)
_SITEMAP_LINE = re.compile(r"^\s*Sitemap:\s*(\S+)\s*$", re.I | re.M)
_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
_URL_ENTRY = re.compile(r"<url>(.*?)</url>", re.I | re.S)
_LASTMOD = re.compile(r"<lastmod>\s*([^<\s]+)\s*</lastmod>", re.I)
_JOB_DETAIL = re.compile(r"/JobDetail/(?:([^/?#]*)/)?(\d+)/?(?:[?#]|$)")
_LOCALE = re.compile(r"/([a-z]{2})_[A-Z]{2}/")

# Posting id (`host:id`) → when this process last read its page.
_SEEN: dict[str, float] = {}
_SEEN_SECONDS = 7 * 24 * 3600
_SEEN_SIZE = 50_000


class Blocked(Exception):
    """The tenant answered with a bot challenge. Left alone, not retried around."""


def parse_spec(spec: str) -> tuple[str, str] | None:
    match = _SPEC.match((spec or "").strip())
    if not match:
        logger.warning("Avature: invalid board spec %r (want host/portal)", spec)
        return None
    return match.group(1).lower(), match.group(2)


def _get(url: str, **kw) -> httpx.Response:
    resp = httpx.get(url, headers=_HEADERS, timeout=_TIMEOUT, **kw)
    if resp.headers.get("x-amzn-waf-action") or (resp.status_code == 202 and not resp.content):
        raise Blocked(f"{urlparse(url).hostname} answered with a bot challenge")
    return resp


def sitemap_index_url(host: str, portal: str) -> str | None:
    """The portal's sitemap as the tenant's robots.txt names it; None when it names none."""
    resp = _get(f"https://{host}/robots.txt", follow_redirects=True)
    resp.raise_for_status()
    wanted = f"/{portal}/sitemap_index.xml".lower()
    for url in _SITEMAP_LINE.findall(resp.text):
        if urlparse(url).path.lower() == wanted:
            return url
    return None


def _pick_sitemap(children: list[str]) -> str:
    """English if the portal has it; each language lists the same postings."""
    for url in children:
        if "/en_US/" in url:
            return url
    for url in children:
        match = _LOCALE.search(url)
        if match and match.group(1) == "en":
            return url
    return children[0]


def _title_from_slug(slug: str) -> str:
    text = unquote(slug or "").replace("-", " ").replace("_", " ")
    text = re.sub(r"\s+", " ", text).strip()
    # Some portals put a number where the title goes.
    return text if re.search(r"[^\W\d_]", text) else ""


def _parse_entries(xml: str) -> list[dict]:
    entries: dict[str, dict] = {}
    for block in _URL_ENTRY.findall(xml):
        loc = _LOC.search(block)
        if not loc:
            continue
        match = _JOB_DETAIL.search(urlparse(loc.group(1)).path + "?")
        if not match:
            continue
        lastmod = _LASTMOD.search(block)
        entries.setdefault(match.group(2), {
            "id": match.group(2),
            "url": loc.group(1),
            "title": _title_from_slug(match.group(1) or ""),
            "lastmod": lastmod.group(1) if lastmod else None,
        })
    return list(entries.values())


def sitemap(host: str, portal: str) -> list[dict] | None:
    """
    Every posting the portal's sitemap lists — id, URL, title from the URL
    (blank when the URL has none) and last-modified date — or None when the
    portal publishes no sitemap.
    """
    index_url = sitemap_index_url(host, portal)
    if not index_url:
        return None
    resp = _get(index_url, follow_redirects=True)
    resp.raise_for_status()
    if "<sitemapindex" in resp.text[:2000]:
        children = _LOC.findall(resp.text)
        if not children:
            return []
        resp = _get(_pick_sitemap(children), follow_redirects=True)
        resp.raise_for_status()
    return _parse_entries(resp.text)


# --- One posting's page ------------------------------------------------------

class _Fields(HTMLParser):
    """
    The page's labelled fields, in order: [(label, text)], label "" when none.

    A field is whatever element carries Avature's field classes — `div`s on
    most portals, `dt`/`dd` on TotalEnergies' — and ends at that element's
    own closing tag. The older template has no fields, only a
    `crmDescription` block (NVA's portal), read as one unlabelled field.
    """

    _BLOCKS = {"p", "br", "li", "div", "h1", "h2", "h3", "h4", "h5", "tr", "ul", "ol", "dd"}
    _CLASSES = (("article__content__view__field__label", "label"),
                ("article__content__view__field__value", "value"),
                ("crmDescription", "value"))

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fields: list[tuple[str, str]] = []
        self.meta: dict[str, str] = {}
        self.title = ""
        self._capture: str | None = None
        self._capture_tag = ""
        self._nesting = 0
        self._buffer: list[str] = []
        self._label: str | None = None
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and (attrs.get("property") or "").startswith("og:"):
            self.meta[attrs["property"]] = attrs.get("content") or ""
        if tag == "title":
            self._in_title = True
        if self._capture is None:
            cls = attrs.get("class") or ""
            kind = next((k for marker, k in self._CLASSES if marker in cls), None)
            if kind:
                self._capture, self._capture_tag, self._nesting, self._buffer = kind, tag, 0, []
                return
        elif tag == self._capture_tag:
            self._nesting += 1
        if self._capture and tag in self._BLOCKS:
            self._buffer.append("\n")
        if self._capture and tag == "li":
            self._buffer.append("- ")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if not self._capture or tag != self._capture_tag:
            return
        if self._nesting:
            self._nesting -= 1
            return
        text = re.sub(r"[ \t\xa0]+", " ", "".join(self._buffer))
        text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
        if self._capture == "label":
            self._label = text
        else:
            self.fields.append((self._label or "", text))
            self._label = None
        self._capture = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._capture:
            self._buffer.append(data)


# Each portal labels its own fields ("Location", "Location(s)", "Job City",
# "Office Location", "Advertising location"; "Date Published", "Posted date",
# "Job Requsition Published Date"), so labels are matched loosely, after the
# trailing colon is gone.
_LOCATION_LABEL = re.compile(
    r"^(?:job |work |office |primary |advertising |posting )?locations?(?: ?\(s\))?$", re.I)
_PLACE_PARTS = (
    re.compile(r"^(?:job |location |work )?city$", re.I),
    re.compile(r"^(?:job |location )?(?:state|province|region)$", re.I),
    re.compile(r"^(?:job |location )?country$", re.I),
)
_COMPANY_LABEL = re.compile(r"^(?:company|organi[sz]ation|legal entity|employer)$", re.I)
_POSTED_LABEL = re.compile(r"posted|publish|publication|^(?:date|fecha)$", re.I)
_TYPE_LABEL = re.compile(r"type$|^(?:pay class|working time|schedule|job status|hours)$", re.I)
_ARRANGEMENT_LABEL = re.compile(r"arrangement|workplace|work location type|remote", re.I)
_BODY_LABEL = re.compile(
    r"descri|responsib|requirement|qualification|about|dut(?:y|ies)|summary|overview|"
    r"benefit|what you|who you|role", re.I)
# Template defaults some portals leave in `og:site_name`.
_GENERIC_NAMES = {"company", "careers", "jobs", "career site", "avature"}
_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d-%B-%Y",
                 "%b %d, %Y", "%B %d, %Y", "%A, %B %d, %Y", "%d %b %Y", "%d %B %Y", "%d.%m.%Y")


def _label(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().rstrip(":").strip())


def _date(text: str | None) -> str | None:
    text = re.sub(r"\s+", " ", (text or "").strip())
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return None


def _kind(text: str) -> str | None:
    """Employment type out of whatever the portal wrote ("Full-time (30+ hrs/week)")."""
    text = (text or "").lower()
    for pattern, kind in ((r"\bintern", "internship"), (r"part[- ]?time", "part_time"),
                          (r"full[- ]?time", "full_time"),
                          (r"\b(?:contract|temporary|fixed[- ]term)", "contract")):
        if re.search(pattern, text):
            return kind
    return None


def parse_detail(html: str) -> dict | None:
    """Title, location, company, description and dates out of one posting page."""
    parser = _Fields()
    try:
        parser.feed(html or "")
    except Exception:
        return None
    # `og:title` is sometimes escaped twice ("Crane &amp;amp; Rigging").
    title = unescape((parser.meta.get("og:title") or "").strip()).strip()
    if not title:
        # "<Title> - <id> - <Company>"
        title = re.split(r"\s+-\s+", parser.title.strip())[0].strip()
    fields = [(_label(label), value) for label, value in parser.fields]
    # Some portals also publish the posting for Google's job results (DTH's).
    from app.services.enrichment import json_ld_extraction

    ld = json_ld_extraction(html)
    ld_details = ld.details or {}
    if not title or not (fields or ld.description):
        return None

    def first(pattern, exclude=None, read=None):
        """The first field so labelled (that `read` can make sense of)."""
        for label, value in fields:
            if label and pattern.search(label) and not (exclude and exclude.search(label)):
                got = read(value) if read else value
                if got:
                    return got
        return None if read else ""

    location = first(_LOCATION_LABEL)
    if location:
        places = [p.strip(" -") for p in re.split(r"\n|\s\|\s", location) if p.strip(" -")]
    else:
        # "City / State / Country" as separate fields.
        parts = [first(pattern) for pattern in _PLACE_PARTS]
        places = [", ".join(p for p in parts if p)] if any(parts) else []
    if not places and ld_details.get("location"):
        places = [ld_details["location"]]
    company = first(_COMPANY_LABEL)
    site = (parser.meta.get("og:site_name") or "").strip()
    if not company and site.lower() not in _GENERIC_NAMES:
        company = site
    body = []
    for label, value in fields:
        if value == title or not value:
            continue
        if label and (_LOCATION_LABEL.search(label) or _COMPANY_LABEL.search(label)):
            continue
        if not label:
            body.append(value)
        elif _BODY_LABEL.search(label) or len(value) >= 200:
            body.append(value if len(value) >= 200 else f"{label}\n{value}")
    arrangement = first(_ARRANGEMENT_LABEL)
    return {
        "title": title,
        "location": "; ".join(dict.fromkeys(places)),
        "company": company,
        "description": "\n\n".join(body) or ld.description or "",
        "posted_at": (first(_POSTED_LABEL, exclude=re.compile(r"clos|expir|end", re.I), read=_date)
                      or ld.posted_at),
        "employment_type": (first(_TYPE_LABEL, exclude=_ARRANGEMENT_LABEL, read=_kind)
                            or ld_details.get("employment_type") or _kind(title)),
        "remote": "remote" in f"{arrangement} {location}".lower(),
    }


def detail(url: str) -> dict | None:
    """
    One posting's page, parsed; None when it isn't one. A closed posting
    redirects to the portal's `Error` page and an internal one to `Login`;
    some portals first redirect a posting to itself to set a session cookie,
    which is followed.
    """
    resp = _get(url, follow_redirects=True)
    if "/JobDetail/" not in resp.url.path:
        return None
    resp.raise_for_status()
    return parse_detail(resp.text)


# --- The cycle ---------------------------------------------------------------

def _lastmod_at(entry: dict) -> datetime | None:
    raw = entry.get("lastmod") or ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _remember(key: str, when: float) -> None:
    if len(_SEEN) >= _SEEN_SIZE:
        for stale in sorted(_SEEN, key=_SEEN.get)[: _SEEN_SIZE // 10]:
            del _SEEN[stale]
    _SEEN[key] = when


def _as_job(host: str, spec: str, entry: dict, page: dict) -> dict:
    title, location = page["title"], page["location"]
    description = clean_description(page["description"])
    intern = page["employment_type"] == "internship" or bool(re.search(r"\bintern", title, re.I))
    lastmod = _lastmod_at(entry)
    return {
        "source": "avature",
        "source_job_id": f"{host}:{entry['id']}",
        "title": title,
        # The page's company (Koch's portal names Molex, Guardian…); failing
        # that, the board spec, which the registry swaps for its name.
        "company": page["company"] or spec,
        "location": location,
        "is_remote": page["remote"] or "remote" in f"{title} {location}".lower(),
        "url": entry["url"],
        "description": description,
        "experience_level": parse_experience_level(title, description),
        "posted_at": page["posted_at"] or (lastmod.isoformat() if lastmod else None),
        "employment_type": "internship" if intern else page["employment_type"],
    }


def fetch(company_slugs: list[str], queries: list[str] | None = None,
          max_age_days=None) -> list[dict]:
    """Each portal's sitemap, and the pages of the new postings matching the roles."""
    from app.services.matcher import title_priority_match

    queries = [q for q in dict.fromkeys(queries or []) if q and q.strip()]
    if not queries:
        return []
    # Read here, in the calling thread: the board workers don't see the cycle.
    cfg = cycle_cfg()
    if max_age_days is None:
        max_age_days = getattr(cfg, "MAX_JOB_AGE_DAYS", None)
    cutoff = age_cutoff(max_age_days)
    try:
        cap = max(0, int(getattr(cfg, "AVATURE_MAX_DETAILS", DEFAULT_MAX_DETAILS)))
    except (TypeError, ValueError):
        cap = DEFAULT_MAX_DETAILS
    known = described("avature")
    now = time.monotonic()

    def fresh(key: str) -> bool:
        seen_at = _SEEN.get(key)
        return key not in known and (seen_at is None or now - seen_at > _SEEN_SECONDS)

    def _fetch_one(spec: str) -> list[dict]:
        parsed = parse_spec(spec)
        if not parsed:
            return []
        host, portal = parsed
        entries = sitemap(host, portal)
        if entries is None:
            logger.info("Avature: %s publishes no sitemap", spec)
            return []
        saw_postings(f"{host}:{e['id']}" for e in entries)

        candidates = []
        for entry in entries:
            key = f"{host}:{entry['id']}"
            dated = _lastmod_at(entry)
            if cutoff is not None and dated is not None and dated < cutoff:
                continue
            if not fresh(key):
                continue
            # A title in the URL is judged now; a number is read to find out.
            if entry["title"] and not title_priority_match(entry["title"], queries):
                continue
            candidates.append(entry)
        # Titled matches first, then newest.
        candidates.sort(key=lambda e: (not e["title"], -(_lastmod_at(e) or datetime.min.replace(
            tzinfo=timezone.utc)).timestamp()))

        jobs = []
        for entry in candidates[:cap]:
            key = f"{host}:{entry['id']}"
            try:
                page = detail(entry["url"])
            except Blocked:
                raise
            except Exception as exc:
                logger.warning("Avature posting %s: %s", entry["url"], exc)
                continue
            _remember(key, now)
            if not page or not title_priority_match(page["title"], queries):
                continue
            jobs.append(_as_job(host, spec, entry, page))
        return jobs

    return fetch_boards_concurrently(company_slugs, _fetch_one, "Avature", board_workers())
