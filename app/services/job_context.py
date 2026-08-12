"""
What we already know about the posting someone is looking at.

The overlay's whole job is to answer three questions before you spend twenty
minutes on an application: have I seen this, what did it score, and did I
already apply. All three are in the database already — the difficulty is only
that the browser knows a posting by its URL, and one posting has many.

The same job arrives as an Adzuna redirect, as the Greenhouse link that redirect
resolved to, and as whatever LinkedIn puts in the address bar with three
tracking parameters attached. So lookup tries a widening set of forms rather
than one, and gives up rather than guessing: reporting "not seen" for a job we
have is a missed badge, but reporting the wrong job's score would be actively
misleading.
"""

import logging
import re
from urllib.parse import urlparse, urlunparse

from app.models.application import Application
from app.models.job import Job

logger = logging.getLogger(__name__)

# LinkedIn writes the posting id in several places; any of them is enough to
# rebuild the canonical URL the rest of the system stores.
_LINKEDIN_ID_RE = re.compile(r"linkedin\.com/jobs/view/(\d+)", re.I)
_LINKEDIN_PARAM_RE = re.compile(r"[?&]currentJobId=(\d+)", re.I)


def _strip(url: str) -> str:
    """The URL without query or fragment, which is where tracking lives."""
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def _linkedin_canonical(url: str) -> str | None:
    for pattern in (_LINKEDIN_ID_RE, _LINKEDIN_PARAM_RE):
        match = pattern.search(url)
        if match:
            return f"https://www.linkedin.com/jobs/view/{match.group(1)}/"
    return None


def url_variants(url: str) -> list[str]:
    """
    Every form of this URL worth looking for, most specific first.

    Deliberately a small closed set rather than fuzzy matching. Each variant is
    a form some part of the system is known to store — the exact string, the
    string without tracking parameters, and LinkedIn's canonical shape — so a
    match means the same posting rather than a similar one.
    """
    url = (url or "").strip()
    if not url:
        return []

    variants = [url]
    stripped = _strip(url)
    for candidate in (stripped, stripped.rstrip("/"), stripped.rstrip("/") + "/"):
        if candidate and candidate not in variants:
            variants.append(candidate)

    canonical = _linkedin_canonical(url)
    if canonical and canonical not in variants:
        variants.append(canonical)
        bare = canonical.rstrip("/")
        if bare not in variants:
            variants.append(bare)
    return variants


def find_job(db, url: str) -> Job | None:
    """The stored job this URL refers to, or None."""
    variants = url_variants(url)
    if not variants:
        return None

    job = (
        db.query(Job)
        .filter(Job.url.in_(variants))
        .first()
    )
    if job:
        return job

    job = db.query(Job).filter(Job.apply_url.in_(variants)).first()
    if job:
        return job

    # source_urls accumulates every address a posting was seen at, which is
    # exactly this question, so it is worth the array scan as a last resort.
    return db.query(Job).filter(Job.source_urls.overlap(variants)).first()


def _score(job: Job) -> int | None:
    if job.llm_score is not None:
        return int(job.llm_score)
    if job.keyword_score is not None:
        return int(job.keyword_score)
    return None


def context(db, url: str) -> dict:
    """
    Everything the overlay needs about this posting, in one call.

    Shaped for display rather than for completeness: the caller renders it
    directly, so anything that would need interpreting on the client is
    resolved here.
    """
    job = find_job(db, url)
    if not job:
        return {"known": False}

    application = (
        db.query(Application)
        .filter(Application.job_id == job.id)
        .order_by(Application.created_at.desc())
        .first()
    )

    return {
        "known": True,
        "job": {
            "id": str(job.id),
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "score": _score(job),
            "matched_by": job.matched_by,
            "status": job.status.value if job.status else None,
            "filter_reason": job.filter_reason,
            "filter_detail": job.filter_detail,
            "sponsorship_direction": job.sponsorship_direction,
            "sponsorship_note": job.sponsorship_note,
            "matched_skills": list(job.matched_skills or [])[:8],
            "missing_skills": list(job.missing_skills or [])[:8],
            "reasoning": (job.llm_reasoning or "")[:400],
        },
        "application": (
            {
                "id": str(application.id),
                "status": application.status.value if application.status else None,
                "applied_at": (
                    application.applied_at.isoformat() if application.applied_at else None
                ),
            }
            if application
            else None
        ),
        # Deep link so the overlay can hand off rather than reimplement the app.
        "path": f"/jobs/{job.id}/application",
    }


def prepare(db, url: str, posting: dict | None = None) -> dict:
    """
    Get this posting to the point where documents are being written for it.

    Find or store the job, find or open its application, and say which. Queuing
    the generation itself is the router's job — it needs Celery, and this stays
    callable from a test without one.
    """
    from app.services.harvest import HARVEST_SOURCE, save_harvested_jobs

    job = find_job(db, url)
    if not job:
        posting = posting or {}
        title = (posting.get("title") or "").strip()
        company = (posting.get("company") or "").strip()
        if not (title and company):
            # Without these there is nothing to match against and nothing to
            # write a resume for; a placeholder row would be worse than none.
            return {"ok": False, "detail": "That posting has no title or company to save."}

        save_harvested_jobs(db, [{
            "source": HARVEST_SOURCE,
            "source_job_id": posting.get("source_job_id"),
            "url": url,
            "title": title,
            "company": company,
            "location": posting.get("location") or "",
            "description": posting.get("description") or "",
            "is_remote": bool(posting.get("is_remote")),
        }])
        job = find_job(db, url)
        if not job:
            return {"ok": False, "detail": "Could not store that posting."}

    application = job.applications[0] if job.applications else None
    created = False
    if not application:
        application = Application(job_id=job.id)
        db.add(application)
        db.commit()
        db.refresh(application)
        created = True

    return {
        "ok": True,
        "job_id": str(job.id),
        "application_id": str(application.id),
        "created_application": created,
        "path": f"/apps/{application.id}",
    }
