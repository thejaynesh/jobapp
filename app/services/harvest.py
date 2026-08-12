"""
Jobs the browser saw, handed over without anybody fetching anything.

When you browse LinkedIn normally, the page asks its own API for job cards and
gets back far more than it renders — full descriptions, applicant counts, salary
bands. A content script reads those responses as they arrive and posts them
here. No extra requests are made, so there is nothing to rate-limit and nothing
to detect; the traffic is a person using the site.

This matters most for LinkedIn specifically. The guest API the server polls
returns ten cards a page and needs a separate request per description, which is
what makes `LINKEDIN_MAX_DETAIL_FETCHES` the real ceiling on that source.
Voyager returns descriptions inline, so the ceiling disappears.

Parsing is deliberately shape-based rather than path-based
------------------------------------------------------------
The obvious implementation reads `elements[].jobCardUnion.*.jobPosting.title`.
That breaks the first time LinkedIn reorganizes its response, and it breaks
silently — an empty harvest looks identical to an idle browser.

So instead this walks the whole payload and picks out any object that *looks*
like a job: something with a title, a company, and an id or a URL. Field names
are matched from a list of aliases. A redesign that moves the nesting around
keeps working; only a rename of every field at once would defeat it, and that
is exactly the kind of change that shows up as a sudden drop to zero rather
than as quiet corruption.
"""

import logging
import re
from datetime import datetime, timezone

from app.models.job import Job, JobStatus
from app.services.deduplication import (
    compute_dedupe_hash,
    find_existing_job,
    merge_or_skip,
)

logger = logging.getLogger(__name__)

# Where harvested jobs say they came from. Its own source name so the yield is
# visible next to the API sources rather than blended into them.
HARVEST_SOURCE = "linkedin_harvest"

# Field aliases, most specific first. Several are checked because one payload
# calls it `companyName` and another nests it under `companyDetails`.
_TITLE_KEYS = ("title", "jobTitle", "jobPostingTitle", "name")
_COMPANY_KEYS = (
    "companyName", "company", "companyUrn", "primarySubtitle", "subtitle",
)
_LOCATION_KEYS = (
    "formattedLocation", "locationName", "location", "secondarySubtitle",
    "secondaryDescription",
)
_DESCRIPTION_KEYS = ("description", "jobDescription", "descriptionText")
_URL_KEYS = ("jobPostingUrl", "applyUrl", "companyApplyUrl", "url", "link")
_ID_KEYS = ("jobPostingId", "entityUrn", "trackingUrn", "referenceId", "id")
_REMOTE_KEYS = ("workplaceType", "workRemoteAllowed", "workplaceTypes")

# LinkedIn ids arrive as bare numbers or wrapped in an urn.
_URN_ID_RE = re.compile(r"(\d{6,})")

# A payload nests deeply; without a ceiling a cyclic or pathological structure
# would walk forever.
_MAX_DEPTH = 12
_MAX_NODES = 20000


def _text(value) -> str:
    """
    A string out of whatever shape the field arrived in.

    Voyager writes rich text as `{"text": "...", "attributes": [...]}`, and
    company names sometimes as `{"name": "..."}`.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in ("text", "name", "localizedName", "title"):
            if key in value:
                return _text(value[key])
    if isinstance(value, list) and value:
        return _text(value[0])
    return ""


def _first(node: dict, keys: tuple) -> str:
    for key in keys:
        if key in node:
            found = _text(node[key])
            if found:
                return found
    return ""


def _job_id(node: dict) -> str:
    for key in _ID_KEYS:
        raw = _text(node.get(key))
        if not raw:
            continue
        match = _URN_ID_RE.search(raw)
        if match:
            return match.group(1)
        if raw.isdigit():
            return raw
    return ""


def _is_remote(node: dict) -> bool:
    for key in _REMOTE_KEYS:
        value = node.get(key)
        if isinstance(value, bool):
            return value
        text = _text(value).lower()
        if "remote" in text:
            return True
    return False


def _walk(node, depth: int = 0, budget: list | None = None):
    """Every dict anywhere in the payload, depth- and size-capped."""
    if budget is None:
        budget = [_MAX_NODES]
    if depth > _MAX_DEPTH or budget[0] <= 0:
        return
    budget[0] -= 1

    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value, depth + 1, budget)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item, depth + 1, budget)


def _looks_like_job(node: dict) -> bool:
    """
    A title and a company, plus something to identify it by.

    All three are required together on purpose. A title alone matches every
    heading in the payload, and a company alone matches the sidebar.
    """
    if not isinstance(node, dict):
        return False
    if not _first(node, _TITLE_KEYS):
        return False
    if not _first(node, _COMPANY_KEYS):
        return False
    return bool(_job_id(node) or _first(node, _URL_KEYS))


def _normalize(node: dict) -> dict | None:
    title = _first(node, _TITLE_KEYS)
    company = _first(node, _COMPANY_KEYS)
    if not title or not company:
        return None

    job_id = _job_id(node)
    url = _first(node, _URL_KEYS)
    if not url and job_id:
        # Reconstructing beats dropping the job: this URL shape has been stable
        # for years and is what the site itself links to.
        url = f"https://www.linkedin.com/jobs/view/{job_id}/"
    if not url:
        return None

    return {
        "source": HARVEST_SOURCE,
        "source_job_id": job_id or None,
        "url": url,
        "title": title,
        "company": company,
        "location": _first(node, _LOCATION_KEYS),
        "description": _first(node, _DESCRIPTION_KEYS),
        "is_remote": _is_remote(node),
    }


def extract_jobs(payload) -> list[dict]:
    """
    Every job-shaped object in an intercepted API response.

    Deduplicated within the payload: the same posting commonly appears in both
    a card list and a detail blob in one response.
    """
    if not isinstance(payload, (dict, list)):
        return []

    found: dict[str, dict] = {}
    for node in _walk(payload):
        if not _looks_like_job(node):
            continue
        job = _normalize(node)
        if not job:
            continue
        key = job["source_job_id"] or job["url"]
        existing = found.get(key)
        # Keep the richest copy. A card and a detail blob for the same posting
        # differ mostly in whether the description came along.
        if not existing or len(job["description"]) > len(existing["description"]):
            found[key] = job
    return list(found.values())


def save_harvested_jobs(db, jobs: list[dict]) -> dict:
    """
    Store harvested jobs through the same dedupe rules as fetched ones.

    Orchestration is separate from the fetch cycle's rather than shared with it,
    because the two want different things: there is no staleness filter here (a
    posting the user is looking at right now is current by definition) and no
    per-source budget. The dedupe primitives underneath are the same ones, so a
    harvested job and a fetched job still collapse into one row.
    """
    counts = {"inserted": 0, "merged": 0, "skipped": 0, "invalid": 0}
    now = datetime.now(timezone.utc)

    for data in jobs:
        title = (data.get("title") or "").strip()
        company = (data.get("company") or "").strip()
        url = (data.get("url") or "").strip()
        if not (title and company and url):
            counts["invalid"] += 1
            continue

        location = (data.get("location") or "").strip()
        description = data.get("description") or ""
        source_job_id = data.get("source_job_id")
        dedupe_hash = compute_dedupe_hash(company, title, location)

        existing = find_existing_job(
            db, HARVEST_SOURCE, url, source_job_id, dedupe_hash
        )
        if existing is not None:
            # The harvested copy usually carries a fuller description than the
            # guest API managed, which is the main reason this path exists.
            if url in existing.source_urls or (
                source_job_id
                and existing.source_job_id == source_job_id
                and existing.source == HARVEST_SOURCE
            ):
                if description and len(description) > len(existing.description or ""):
                    existing.description = description
                    counts["merged"] += 1
                else:
                    counts["skipped"] += 1
                continue
            merge_or_skip(db, existing, url, description, layer=3)
            counts["merged"] += 1
            continue

        db.add(
            Job(
                source=HARVEST_SOURCE,
                source_job_id=source_job_id,
                source_urls=[url],
                title=title,
                company=company,
                location=location,
                is_remote=bool(data.get("is_remote")),
                url=url,
                description=description,
                experience_level="mid",
                status=JobStatus.new,
                fetched_at=now,
                dedupe_hash=dedupe_hash,
            )
        )
        counts["inserted"] += 1

    db.commit()
    if counts["inserted"] or counts["merged"]:
        logger.info(
            "harvest: %d new, %d enriched, %d already known",
            counts["inserted"], counts["merged"], counts["skipped"],
        )
    return counts
