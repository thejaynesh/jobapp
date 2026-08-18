"""
Going back for the description the source did not send.

Every job row carries a `url` (and often an `apply_url`) where the full posting
lives, and until now nothing ever went and got it. That is the single biggest
data problem in the system: Adzuna is half the database and its API truncates
every description at exactly 500 characters; LinkedIn ships 90% of its jobs
with no description at all; Jooble sends ~300-character teasers. Around 25,000
jobs have been auto-rejected for "too few skills" or "no description" — that is,
for having thin data rather than for being bad jobs.

Four methods, tried cheapest and most reliable first:

1. **ATS API by URL pattern.** If the link points at Greenhouse, Lever, Ashby,
   SmartRecruiters, Workable or Workday, the clean job description is one JSON
   request away. No scraping, no parsing markup, no model call.
2. **JSON-LD.** Any page that wants to appear in Google's job results publishes
   a `JobPosting` block. One parse gives description, date, salary and location
   together. (The extension's overlay already does exactly this in the browser;
   this is the same read, server-side.)
3. **LLM extraction.** The page as text, handed to a model with "return the job
   description as JSON, or null if this is not a job posting". Calls are free,
   so this runs on anything the first two could not read.
4. **The browser.** Hosts that answer a datacenter IP with a challenge — the
   LinkedIn/Dice tier — get queued as a `resolve_link` task, and the extension
   returns the HTML from a real browser on a residential connection. Methods 2
   and 3 then run on what comes back.

There is no request budget, on purpose: the backlog is tens of thousands of
jobs and a budget that cannot drain it never drains it. What there is instead is
politeness — a cap on concurrent requests per host, and a gap between them —
so the backlog takes days rather than making anybody's afternoon unpleasant.
"""

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.models.job import Job, JobStatus
from app.services.descriptions import clean
from app.services.link_resolver import _HEADERS, _HostLimiter

logger = logging.getLogger(__name__)

# Below this, a description is a teaser rather than a posting: Adzuna's stubs
# are 500 characters and Jooble's are around 300, and both cut off mid-sentence.
THIN_DESCRIPTION_CHARS = 1500

# A result has to beat what we already have by this much to be worth writing.
# Re-fetching the same 500-character stub and storing it again would count as
# work done while changing nothing.
MIN_IMPROVEMENT_CHARS = 200

# Filter reasons that mean "we judged this on data we didn't have". These two
# name the missing data outright, which is why enrichment goes looking for
# their jobs first — see `select_targets`.
RESCUABLE_FILTER_REASONS = ("no_description", "few_skills")

DEFAULT_TIMEOUT = 15
_MAX_PAGE_CHARS = 300_000
_MAX_LLM_CHARS = 24_000


# ---------------------------------------------------------------------------
# What one attempt produced
# ---------------------------------------------------------------------------

@dataclass
class Extraction:
    """A description, and whatever else the page happened to say."""
    description: str = ""
    method: str = ""
    posted_at: str | None = None
    details: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.description)


# ---------------------------------------------------------------------------
# 1. ATS APIs
# ---------------------------------------------------------------------------

_GREENHOUSE_URL = re.compile(
    r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_app\?for=)?"
    r"([A-Za-z0-9_.-]+)/jobs/(\d+)", re.I,
)
_GREENHOUSE_EMBED = re.compile(
    r"greenhouse\.io/embed/job_app\?for=([A-Za-z0-9_.-]+)&(?:amp;)?token=(\d+)", re.I,
)
_LEVER_URL = re.compile(r"jobs\.lever\.co/([A-Za-z0-9_.-]+)/([0-9a-f-]{36})", re.I)
_ASHBY_URL = re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.-]+)/([0-9a-f-]{36})", re.I)
_SMARTRECRUITERS_URL = re.compile(
    r"jobs\.smartrecruiters\.com/([A-Za-z0-9_.-]+)/(\d+)", re.I
)
_WORKABLE_URL = re.compile(
    r"apply\.workable\.com/([A-Za-z0-9_.-]+)/j/([0-9A-Za-z]+)", re.I
)
_WORKDAY_URL = re.compile(
    r"([A-Za-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[A-Za-z-]+/)?"
    r"([A-Za-z0-9_-]+)(/job/[^?#]+)", re.I,
)


def _get_json(client: httpx.Client, url: str) -> dict | list | None:
    resp = client.get(url, headers={"Accept": "application/json"})
    resp.raise_for_status()
    return resp.json()


def _greenhouse(client: httpx.Client, slug: str, job_id: str) -> Extraction:
    data = _get_json(
        client, f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}"
    )
    if not isinstance(data, dict):
        return Extraction()
    # `content` is HTML, and Greenhouse escapes it before putting it in JSON —
    # which is exactly the double-escaped shape `clean` was written for.
    return Extraction(
        description=clean(data.get("content") or ""),
        method="ats_api",
        posted_at=data.get("first_published") or data.get("updated_at"),
        details={"location": (data.get("location") or {}).get("name") or ""},
    )


def _lever(client: httpx.Client, slug: str, posting_id: str) -> Extraction:
    data = _get_json(client, f"https://api.lever.co/v0/postings/{slug}/{posting_id}")
    if not isinstance(data, dict):
        return Extraction()
    # descriptionPlain is only the opening section; the lists that carry the
    # requirements live in `lists`, and dropping them is how a Lever job ends
    # up looking like two paragraphs of marketing.
    parts = [data.get("descriptionPlain") or data.get("description") or ""]
    for block in data.get("lists") or []:
        parts.append((block.get("text") or "").strip())
        parts.append(block.get("content") or "")
    parts.append(data.get("additionalPlain") or data.get("additional") or "")
    return Extraction(
        description=clean("\n\n".join(p for p in parts if p)),
        method="ats_api",
        posted_at=data.get("createdAt"),
    )


def _ashby(client: httpx.Client, slug: str, posting_id: str) -> Extraction:
    # Ashby publishes a board, not a posting endpoint, so the whole board comes
    # back and the id is found in it. Cheap enough: one request serves every
    # job of that company in the same pass.
    data = _get_json(
        client, f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    )
    if not isinstance(data, dict):
        return Extraction()
    for item in data.get("jobs") or []:
        if str(item.get("id") or "").lower() != posting_id.lower():
            continue
        return Extraction(
            description=clean(
                item.get("descriptionPlain") or item.get("descriptionHtml") or ""
            ),
            method="ats_api",
            posted_at=item.get("publishedAt"),
            details={"location": item.get("location") or ""},
        )
    return Extraction()


def _smartrecruiters(client: httpx.Client, slug: str, posting_id: str) -> Extraction:
    data = _get_json(
        client,
        f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{posting_id}",
    )
    if not isinstance(data, dict):
        return Extraction()
    sections = ((data.get("jobAd") or {}).get("sections") or {})
    parts = [
        (sections.get(name) or {}).get("text") or ""
        for name in ("companyDescription", "jobDescription", "qualifications",
                     "additionalInformation")
    ]
    return Extraction(
        description=clean("\n\n".join(p for p in parts if p)),
        method="ats_api",
        posted_at=data.get("releasedDate"),
    )


def _workable(client: httpx.Client, slug: str, shortcode: str) -> Extraction:
    data = _get_json(
        client,
        f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true",
    )
    if not isinstance(data, dict):
        return Extraction()
    for item in data.get("jobs") or []:
        if str(item.get("shortcode") or "").lower() != shortcode.lower():
            continue
        parts = [item.get("description") or "", item.get("requirements") or "",
                 item.get("benefits") or ""]
        return Extraction(
            description=clean("\n\n".join(p for p in parts if p)),
            method="ats_api",
            posted_at=item.get("published_on"),
        )
    return Extraction()


def _workday(client: httpx.Client, tenant: str, host: str, site: str,
             path: str) -> Extraction:
    data = _get_json(
        client,
        f"https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{path}",
    )
    if not isinstance(data, dict):
        return Extraction()
    info = data.get("jobPostingInfo") or {}
    return Extraction(
        description=clean(info.get("jobDescription") or ""),
        method="ats_api",
        posted_at=info.get("startDate") or info.get("postedOn"),
        details={"location": info.get("location") or ""},
    )


def _ats_extraction(client: httpx.Client, url: str) -> Extraction:
    """The posting's JSON, when the URL says which ATS is hosting it."""
    for pattern, call in (
        (_GREENHOUSE_EMBED, _greenhouse),
        (_GREENHOUSE_URL, _greenhouse),
        (_LEVER_URL, _lever),
        (_ASHBY_URL, _ashby),
        (_SMARTRECRUITERS_URL, _smartrecruiters),
        (_WORKABLE_URL, _workable),
    ):
        match = pattern.search(url)
        if match:
            return call(client, *match.groups())

    match = _WORKDAY_URL.search(url)
    if match:
        return _workday(client, *match.groups())
    return Extraction()


def looks_like_ats(url: str) -> bool:
    """True when `_ats_extraction` has an endpoint for this URL."""
    return any(
        pattern.search(url or "")
        for pattern in (_GREENHOUSE_EMBED, _GREENHOUSE_URL, _LEVER_URL, _ASHBY_URL,
                        _SMARTRECRUITERS_URL, _WORKABLE_URL, _WORKDAY_URL)
    )


# ---------------------------------------------------------------------------
# 2. JSON-LD
# ---------------------------------------------------------------------------

_LD_BLOCK = re.compile(
    r'<script[^>]+type=["\']?application/ld\+json["\']?[^>]*>(.*?)</script>',
    re.I | re.S,
)


def _walk_ld(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_ld(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_ld(item)


def _is_job_posting(node: dict) -> bool:
    raw = node.get("@type")
    types = raw if isinstance(raw, list) else [raw]
    return any(str(t).lower() == "jobposting" for t in types if t)


def _ld_blocks(html: str):
    """Every parsed ld+json payload on the page."""
    for block in _LD_BLOCK.finditer(html or ""):
        raw = block.group(1).strip()
        if not raw:
            continue
        try:
            yield json.loads(raw)
        except Exception:
            # Some sites emit JS-with-comments in an ld+json tag. One repair
            # attempt, then move on.
            try:
                yield json.loads(re.sub(r"//[^\n]*", "", raw))
            except Exception:
                continue


def _ld_text(value) -> str:
    """A display string out of a value that may be an object or a list."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("name", "title", "legalName"):
            if key in value:
                return _ld_text(value[key])
        return ""
    if isinstance(value, list):
        for item in value:
            found = _ld_text(item)
            if found:
                return found
    return ""


def json_ld_postings(html: str) -> list[dict]:
    """
    Every `JobPosting` on a listing page, shaped like a source adapter's output.

    Board software that publishes structured data for Google's job results
    publishes it whether or not it also offers an API — which makes this the
    most durable way to read an ATS whose JSON endpoint is undocumented or
    moves around. Descriptions are often absent from a *listing* page's blocks;
    that is fine, because enrichment fetches them from the posting URL
    afterwards.
    """
    found: dict[str, dict] = {}
    for data in _ld_blocks(html):
        for node in _walk_ld(data):
            if not isinstance(node, dict) or not _is_job_posting(node):
                continue
            title = _ld_text(node.get("title"))
            url = _ld_text(node.get("url")) or _ld_text(node.get("sameAs"))
            if not title or not url:
                continue
            details = _details_from_ld(node)
            posting = {
                "title": title,
                "company": _ld_text(node.get("hiringOrganization")),
                "location": details.get("location", ""),
                "url": url,
                "description": clean(node.get("description") or ""),
                "posted_at": node.get("datePosted"),
                "employment_type": details.get("employment_type"),
                "salary_min": details.get("salary_min"),
                "salary_max": details.get("salary_max"),
                "salary_currency": details.get("salary_currency"),
            }
            # Keep the richest copy: a page often carries the same posting in a
            # summary block and again in a fuller one.
            existing = found.get(url)
            if not existing or len(posting["description"]) > len(existing["description"]):
                found[url] = posting
    return list(found.values())


def json_ld_extraction(html: str) -> Extraction:
    """
    The `JobPosting` block any page that wants Google's job results publishes.

    One parse gives description, date, salary, employment type and location
    together — which is why it sits ahead of the model in the order: it is both
    cheaper and more precise than reading prose.
    """
    if not html:
        return Extraction()

    for data in _ld_blocks(html):
        for node in _walk_ld(data):
            if not isinstance(node, dict) or not _is_job_posting(node):
                continue
            description = clean(node.get("description") or "")
            if not description:
                continue
            return Extraction(
                description=description,
                method="json_ld",
                posted_at=node.get("datePosted"),
                details=_details_from_ld(node),
            )
    return Extraction()


def _details_from_ld(node: dict) -> dict:
    """Salary, employment type and location, when the block states them."""
    details: dict = {}

    employment = node.get("employmentType")
    if isinstance(employment, list):
        employment = employment[0] if employment else None
    if employment:
        details["employment_type"] = str(employment)

    salary = node.get("baseSalary")
    if isinstance(salary, dict):
        value = salary.get("value")
        if isinstance(value, dict):
            low, high = value.get("minValue"), value.get("maxValue")
            single = value.get("value")
            if low is not None:
                details["salary_min"] = low
            if high is not None:
                details["salary_max"] = high
            if low is None and high is None and single is not None:
                details["salary_min"] = details["salary_max"] = single
            if salary.get("currency"):
                details["salary_currency"] = str(salary["currency"])[:8]

    location = node.get("jobLocation")
    if isinstance(location, list):
        location = location[0] if location else None
    if isinstance(location, dict):
        address = location.get("address") or {}
        if isinstance(address, dict):
            parts = [address.get("addressLocality"), address.get("addressRegion")]
            joined = ", ".join(p for p in parts if p)
            if joined:
                details["location"] = joined
    return details


# ---------------------------------------------------------------------------
# 3. LLM extraction
# ---------------------------------------------------------------------------

_LLM_PROMPT = (
    "You are extracting a job posting from the text of a web page.\n\n"
    "Return ONLY a JSON object, no prose, with exactly these keys:\n"
    '  "is_job_posting": true or false\n'
    '  "description": the full job description as plain text — responsibilities, '
    "requirements, qualifications, benefits — copied from the page, not "
    "summarised. Empty string if there is none.\n"
    '  "employment_type": one of full_time, part_time, contract, internship, '
    "or null\n"
    '  "salary_min", "salary_max": numbers the posting states, or null. Never '
    "guess or convert.\n"
    '  "salary_currency": ISO code, or null\n\n'
    "If the page is a search results list, a login wall, a bot check, or "
    'anything that is not one job posting, return {"is_job_posting": false} '
    "and nothing else."
)

_JSON_OBJECT = re.compile(r"\{.*\}", re.S)


def _parse_json_object(text: str) -> dict | None:
    """The JSON object in a reply that may be wrapped in prose or fences."""
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    match = _JSON_OBJECT.search(text)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def llm_extraction(html: str, job_id=None) -> Extraction:
    """
    Ask a model to read the page, when nothing structured was there to read.

    Last in the order because it is the least precise, not because it is
    expensive — calls are free here, which is what makes it worth running on
    every page the first two methods could not parse.
    """
    text = clean(html)
    if len(text) < 200:
        return Extraction()

    from app.llm.providers import generation_chat
    from app.services import llm_log

    messages = [
        {"role": "system", "content": _LLM_PROMPT},
        {"role": "user", "content": text[:_MAX_LLM_CHARS]},
    ]
    try:
        with llm_log.stage("enrich_extract", job_id=job_id):
            reply = generation_chat(
                messages,
                api_key=settings.NVIDIA_NIM_API_KEY,
                base_url=settings.NVIDIA_NIM_BASE_URL,
                model=settings.NVIDIA_NIM_MODEL,
                temperature=0.0,
                max_tokens=4096,
            )
    except Exception as exc:
        logger.warning("enrichment: LLM extraction failed: %s", exc)
        return Extraction()

    parsed = _parse_json_object(reply)
    if not parsed or not parsed.get("is_job_posting"):
        return Extraction()

    description = clean(str(parsed.get("description") or ""))
    if not description:
        return Extraction()

    details = {
        key: parsed[key]
        for key in ("employment_type", "salary_min", "salary_max", "salary_currency")
        if parsed.get(key) is not None
    }
    return Extraction(description=description, method="llm", details=details)


# ---------------------------------------------------------------------------
# Putting one job through the chain
# ---------------------------------------------------------------------------

def _fetch_page(client: httpx.Client, url: str) -> str:
    resp = client.get(url)
    resp.raise_for_status()
    if "html" not in resp.headers.get("content-type", "").lower():
        return ""
    return resp.text[:_MAX_PAGE_CHARS]


def extract_from_html(html: str, job_id=None) -> Extraction:
    """Structured data first, then the model. Used on any HTML we hold."""
    found = json_ld_extraction(html)
    if found:
        return found
    return llm_extraction(html, job_id=job_id)


def enrich_one(
    client: httpx.Client, url: str, job_id=None, html: str | None = None,
) -> Extraction:
    """
    Everything we can get for one URL, cheapest method first.

    `html` short-circuits the download when the caller already has the page —
    link resolution hands over the landing HTML it just fetched, and re-fetching
    it would be a request spent on something already in memory.
    """
    if not url:
        return Extraction()

    if html is None and looks_like_ats(url):
        try:
            found = _ats_extraction(client, url)
            if found:
                return found
        except Exception as exc:
            logger.debug("enrichment: ATS API failed for %s: %s", url, exc)

    if html is None:
        html = _fetch_page(client, url)
    if not html:
        return Extraction()

    return extract_from_html(html, job_id=job_id)


# ---------------------------------------------------------------------------
# Choosing what to work on
# ---------------------------------------------------------------------------

def _title_gate(profile_data: dict):
    """
    A predicate that says whether a title is worth spending a request on.

    Enrichment's backlog is bigger than any one pass, so the order matters more
    than the budget: a job whose title the matcher would reject anyway gains
    nothing from a fuller description. Falls open — if the profile has no roles
    yet, everything is a candidate rather than nothing.
    """
    from app.services.matcher import _title_match_roles, _title_matches_roles

    roles = _title_match_roles(profile_data or {})
    if not roles:
        return lambda title: True
    return lambda title: _title_matches_roles(title or "", roles)


def select_targets(db, profile_data: dict | None = None, limit: int = 200) -> list[Job]:
    """
    The jobs most worth going back for, best first.

    Thin *or* missing descriptions, plus the jobs already filtered out for
    exactly that — `no_description` and `few_skills` are verdicts on data we
    never had, and those are the 25,000 rows this feature exists to rescue.

    Title-passing jobs come first. Everything else is ordered newest first,
    because a posting from this morning is more likely to still be open than
    one from six weeks ago.
    """
    from sqlalchemy import func, or_

    from app.services.matcher import DESCRIPTION_DEPENDENT_REASONS

    thin = or_(
        Job.description.is_(None),
        func.length(Job.description) < THIN_DESCRIPTION_CHARS,
    )
    rows = (
        db.query(Job)
        .filter(
            thin,
            Job.closed_at.is_(None),
            or_(
                Job.status != JobStatus.filtered_out,
                # Every verdict that was reached by reading the description,
                # not just the two that name the missing data outright — a job
                # scored 45 on a 500-character stub is as much a victim of thin
                # data as one rejected for having none.
                Job.filter_reason.in_(sorted(DESCRIPTION_DEPENDENT_REASONS)),
            ),
        )
        .order_by(Job.fetched_at.desc())
        # Over-fetch, then sort by title in Python: the title test is a set of
        # regexes over the profile's expanded roles and cannot be expressed in
        # SQL. Bounded so a 150k-row table doesn't arrive in memory.
        .limit(max(limit * 5, limit))
        .all()
    )

    passes_title = _title_gate(profile_data or {})
    ranked = sorted(rows, key=lambda job: (0 if passes_title(job.title) else 1))
    return ranked[:limit]


# ---------------------------------------------------------------------------
# Writing what we found
# ---------------------------------------------------------------------------

def apply_extraction(db, job: Job, found: Extraction) -> dict:
    """
    Store a better description, and let the job be judged again.

    The re-queue is the point of the whole feature. A job filtered out for
    "no description" was never rejected on its merits, so once it has one it
    goes back to `new` and the next matching pass scores it properly.
    """
    outcome = {"improved": False, "chars_gained": 0, "requeued": False}
    if not found or not found.description:
        return outcome

    before = len(job.description or "")
    gained = len(found.description) - before
    if gained < MIN_IMPROVEMENT_CHARS:
        return outcome

    job.description = found.description
    job.description_updated_at = datetime.now(timezone.utc)
    outcome["improved"] = True
    outcome["chars_gained"] = gained

    if found.posted_at and not job.posted_at:
        parsed = _parse_datetime(found.posted_at)
        if parsed:
            job.posted_at = parsed

    location = (found.details or {}).get("location")
    if location and not (job.location or "").strip():
        job.location = str(location)[:255]

    if _worth_rescoring(job):
        job.status = JobStatus.new
        job.filter_reason = None
        job.filter_detail = None
        outcome["requeued"] = True

    return outcome


def _worth_rescoring(job: Job) -> bool:
    """
    Whether a fuller description should send this job back to be scored again.

    Only for verdicts that were reached by reading the description. The big
    one by volume is `low_score`: a job scored 45 on a 500-character stub was
    scored on a teaser, and the real posting routinely tells a different story
    — which is the entire reason enrichment exists.

    Three things are deliberately left alone. A verdict the user made, because
    a fuller description is not grounds for overruling somebody who looked at a
    job and said no. A verdict that never read the description (a title or
    location mismatch), because re-scoring reaches the same answer and costs a
    call. And anything already carrying an application, because that is the
    user's pipeline and re-scoring could strand documents already written for
    it — refreshing those is what roadmap 4.1 is for.
    """
    from app.services.matcher import DESCRIPTION_DEPENDENT_REASONS

    if job.status != JobStatus.filtered_out:
        return False
    if job.filter_reason not in DESCRIPTION_DEPENDENT_REASONS:
        return False
    return not job.applications


def _parse_datetime(raw) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        # Lever and friends report milliseconds; seconds would put every
        # posting in 1970.
        seconds = raw / 1000 if raw > 10_000_000_000 else raw
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except Exception:
            return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# A pass
# ---------------------------------------------------------------------------

@dataclass
class EnrichStats:
    attempted: int = 0
    enriched: int = 0
    unchanged: int = 0
    failed: int = 0
    via: dict = field(default_factory=dict)
    chars_gained: int = 0
    requeued_for_matching: int = 0
    queued_browser: int = 0
    failures_by_host: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "attempted": self.attempted,
            "enriched": self.enriched,
            "unchanged": self.unchanged,
            "failed": self.failed,
            "via": dict(self.via),
            "chars_gained": self.chars_gained,
            "requeued_for_matching": self.requeued_for_matching,
            "queued_browser": self.queued_browser,
            "failures_by_host": dict(self.failures_by_host),
        }


def _host(url: str) -> str:
    return (urlparse(url or "").hostname or "unknown").lower()


# Hosts that answer this server with a challenge however politely we ask. Not
# worth a request from here at all — they go straight to the browser tier.
_BROWSER_ONLY_HOSTS = ("linkedin.com", "indeed.com", "glassdoor.com", "dice.com",
                       "ziprecruiter.com", "wellfound.com")


def _browser_only(url: str) -> bool:
    host = _host(url)
    return any(host == d or host.endswith(f".{d}") for d in _BROWSER_ONLY_HOSTS)


def _target_url(job: Job) -> str:
    """
    Where the full posting lives.

    The apply URL when we have one — it is the employer's own page, and the ATS
    shortcut only works on those — otherwise the listing URL we were given.
    """
    return (job.apply_url or job.url or "").strip()


def enrich_jobs(
    db,
    jobs: list[Job],
    landing_html: dict[str, str] | None = None,
    workers: int | None = None,
    queue_browser: bool = True,
) -> EnrichStats:
    """
    Fetch and store fuller descriptions for `jobs`.

    Requests run concurrently but politely: a per-host limiter caps how many
    land on one site at once and spaces them apart, which is the only budget
    this has. Database writes happen on the calling thread after the fetches,
    because a Session is not safe to share across them.
    """
    stats = EnrichStats()
    if not jobs:
        return stats

    landing_html = landing_html or {}
    workers = workers or getattr(settings, "ENRICH_WORKERS", 8)
    limiter = _HostLimiter(
        max_concurrent=getattr(settings, "ENRICH_PER_HOST", 4),
        min_interval=getattr(settings, "ENRICH_HOST_DELAY_MS", 400) / 1000.0,
    )

    # Split before doing anything: a host that always answers a server with a
    # challenge should cost a queue entry, not a request and a timeout.
    for_browser = [
        job for job in jobs
        if _browser_only(_target_url(job)) and not landing_html.get(job.url or "")
    ]
    browser_ids = {job.id for job in for_browser}
    for_server = [job for job in jobs if job.id not in browser_ids]

    stats.attempted = len(for_server)
    results: list[tuple[Job, Extraction | None, Exception | None]] = []

    if for_server:
        # One client for the pass. httpx.Client is safe to share across threads
        # and pools its connections, which is most of what makes a few hundred
        # requests cheap rather than a few hundred TLS handshakes.
        with httpx.Client(
            headers=_HEADERS, timeout=DEFAULT_TIMEOUT,
            follow_redirects=True, max_redirects=5,
        ) as client:

            def _work(job: Job) -> tuple[Job, Extraction | None, Exception | None]:
                # Landing HTML from link resolution: the page is already in
                # memory, and re-downloading it would be a request spent on
                # something we are holding.
                held = landing_html.get(job.url or "") or None
                try:
                    if held is not None:
                        found = extract_from_html(held, job_id=job.id)
                        if found:
                            found.method = "landing_html"
                        return job, found, None
                    url = _target_url(job)
                    with limiter.slot(url):
                        return job, enrich_one(client, url, job_id=job.id), None
                except Exception as exc:
                    return job, None, exc

            with ThreadPoolExecutor(
                max_workers=max(1, min(workers, len(for_server)))
            ) as pool:
                results = list(pool.map(_work, for_server))

    for job, found, error in results:
        if error is not None:
            stats.failed += 1
            host = _host(_target_url(job))
            stats.failures_by_host[host] = stats.failures_by_host.get(host, 0) + 1
            continue
        if not found:
            stats.unchanged += 1
            continue

        outcome = apply_extraction(db, job, found)
        if not outcome["improved"]:
            stats.unchanged += 1
            continue
        stats.enriched += 1
        stats.chars_gained += outcome["chars_gained"]
        stats.requeued_for_matching += 1 if outcome["requeued"] else 0
        stats.via[found.method] = stats.via.get(found.method, 0) + 1

    try:
        db.commit()
    except Exception as exc:
        logger.error("enrichment: commit failed: %s", exc)
        db.rollback()

    if queue_browser and for_browser:
        stats.queued_browser = queue_for_browser(db, for_browser)

    logger.info(
        "enrichment: %d attempted — %d enriched (+%d chars), %d unchanged, "
        "%d failed, %d queued for the browser, %d back in the matching queue",
        stats.attempted, stats.enriched, stats.chars_gained, stats.unchanged,
        stats.failed, stats.queued_browser, stats.requeued_for_matching,
    )
    return stats


def queue_for_browser(db, jobs: list[Job]) -> int:
    """
    Hand the walled-off hosts to the extension.

    LinkedIn answers this server with a challenge and a real browser with the
    page; the difference is the residential IP and the user's own session,
    which is the entire reason the agent queue exists. Never blocks and never
    fails the pass — if nobody is listening the tasks simply expire.
    """
    from app.services import browser_tasks

    queued = 0
    for job in jobs:
        url = _target_url(job)
        if not url:
            continue
        try:
            browser_tasks.enqueue(
                db, "resolve_link",
                {"url": url, "purpose": "enrich", "job_id": str(job.id)},
                # Below a user pressing a button, above background link tidying.
                priority=2,
                ttl_hours=48,
            )
            queued += 1
        except Exception as exc:
            logger.warning("enrichment: could not queue %s: %s", url, exc)
    if queued:
        logger.info("enrichment: queued %d job(s) for browser enrichment", queued)
    return queued


def run(
    db,
    limit: int | None = None,
    landing_html: dict[str, str] | None = None,
) -> dict:
    """One enrichment pass, recorded in `enrichment_runs`."""
    from app.models.profile import Profile
    from app.services.enrichment_history import record_run

    started_at = datetime.now(timezone.utc)
    limit = limit or getattr(settings, "ENRICH_MAX_PER_RUN", 200)

    profile = db.query(Profile).first()
    profile_data = (profile.data if profile else None) or {}

    error: str | None = None
    try:
        targets = select_targets(db, profile_data, limit=limit)
        stats = enrich_jobs(db, targets, landing_html=landing_html)
    except Exception as exc:
        logger.error("enrichment: pass failed: %s", exc)
        db.rollback()
        error = str(exc)[:500]
        stats = EnrichStats()

    try:
        record_run(db, started_at=started_at, stats=stats, error=error)
        db.commit()
    except Exception as exc:
        logger.error("enrichment: could not record run history: %s", exc)
        db.rollback()

    return stats.as_dict()
