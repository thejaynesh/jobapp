import uuid
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.job import Job, JobStatus
from app.services.locations import REGIONS, REGION_OPTIONS, resolve_region_key
from app.services.matcher import FILTER_REASON_LABELS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["jobs"])
templates = Jinja2Templates(directory="app/templates")

_FILTERABLE_STATUSES = [JobStatus.matched, JobStatus.filtered_out, JobStatus.docs_generated]
_PAGE_SIZE = 50
_SOURCES = ["adzuna", "jsearch", "linkedin", "greenhouse", "lever", "ashby", "handshake", "indeed", "wellfound", "dice", "remotive", "arbeitnow", "remoteok", "weworkremotely"]
_EXP_LEVELS = ["entry", "mid", "senior"]

def _region_clause(region_key: str):
    """Match a job's free-text location against a region's keyword registry.

    Job boards write locations inconsistently ("San Jose, CA", "NYC", "USA"),
    so a region match ORs the region's known city/state/country keywords plus
    a case-sensitive word-boundary regex for 2-letter codes (so ", CA" matches
    but "Canada" doesn't).
    """
    cfg = REGIONS[region_key]
    clauses = [Job.location.ilike(f"%{kw}%") for kw in cfg["keywords"]]
    if cfg["abbrevs"]:
        pattern = r"\y(" + "|".join(cfg["abbrevs"]) + r")\y"
        clauses.append(Job.location.op("~")(pattern))
    return or_(*clauses)


def _filter_reason_counts(db: Session) -> list[tuple[str, int]]:
    """
    How many jobs each filter reason accounts for, biggest first.

    Seeing that 900 of 1000 filtered jobs are `title_mismatch` versus
    `no_description` points at completely different fixes — narrow your target
    roles in the first case, chase a broken source in the second.
    """
    rows = (
        db.query(Job.filter_reason, func.count(Job.id))
        .filter(Job.status == JobStatus.filtered_out,
                Job.filter_reason.isnot(None))
        .group_by(Job.filter_reason)
        .order_by(func.count(Job.id).desc())
        .all()
    )
    return [(reason, int(count)) for reason, count in rows]


_SORT_OPTIONS = {
    "score_desc": Job.llm_score.desc().nullslast(),
    "score_asc": Job.llm_score.asc().nullsfirst(),
    "posted_desc": Job.posted_at.desc().nullslast(),
    "posted_asc": Job.posted_at.asc().nullslast(),
    "company_asc": Job.company.asc(),
}


@router.get("", response_class=HTMLResponse)
def get_jobs(
    request: Request,
    status: str = "",
    q: str = "",
    source: str = "",
    region: str = "",
    location: str = "",
    remote: str = "",
    min_score: str = "",
    exp_level: str = "",
    filter_reason: str = "",
    sort: str = "score_desc",
    page: int = 0,
    db: Session = Depends(get_db),
):
    query = db.query(Job).filter(Job.status.in_(_FILTERABLE_STATUSES))

    if status:
        try:
            query = query.filter(Job.status == JobStatus(status))
        except ValueError:
            pass
    if q:
        query = query.filter(
            (Job.title.ilike(f"%{q}%")) | (Job.company.ilike(f"%{q}%"))
        )
    if source:
        query = query.filter(Job.source == source)
    if region and region in REGIONS:
        query = query.filter(_region_clause(region))
    if location:
        # If the free text names a known region ("united states", "USA", "US"),
        # expand it to the full region match instead of a literal substring —
        # job locations rarely spell the country out.
        region_key = resolve_region_key(location)
        if region_key:
            query = query.filter(_region_clause(region_key))
        else:
            loc_clause = Job.location.ilike(f"%{location}%")
            # "remote" also matches jobs flagged remote whose location
            # string doesn't say so.
            if "remote" in location.lower():
                loc_clause = loc_clause | (Job.is_remote == True)  # noqa: E712
            query = query.filter(loc_clause)
    if remote == "1":
        query = query.filter(Job.is_remote == True)  # noqa: E712
    if exp_level:
        query = query.filter(Job.experience_level == exp_level)
    if min_score:
        try:
            query = query.filter(Job.llm_score >= int(min_score))
        except ValueError:
            pass
    if filter_reason:
        query = query.filter(Job.filter_reason == filter_reason)

    order = _SORT_OPTIONS.get(sort, _SORT_OPTIONS["score_desc"])
    total = query.count()
    jobs = query.order_by(order).offset(page * _PAGE_SIZE).limit(_PAGE_SIZE).all()

    return templates.TemplateResponse(
        "jobs/index.html",
        {
            "request": request,
            "jobs": jobs,
            "filter_reason_filter": filter_reason,
            "filter_reason_counts": _filter_reason_counts(db),
            "filter_reason_labels": FILTER_REASON_LABELS,
            "status_filter": status,
            "q": q,
            "source_filter": source,
            "region_filter": region,
            "location_filter": location,
            "remote_filter": remote,
            "min_score_filter": min_score,
            "exp_level_filter": exp_level,
            "sort": sort,
            "page": page,
            "total": total,
            "page_size": _PAGE_SIZE,
            "has_prev": page > 0,
            "has_next": (page + 1) * _PAGE_SIZE < total,
            "sources": _SOURCES,
            "exp_levels": _EXP_LEVELS,
            "region_options": REGION_OPTIONS,
        },
    )


@router.get("/{job_id}/application")
def open_job_application(job_id: uuid.UUID, db: Session = Depends(get_db)):
    """Open the job's application/docs page, creating the application if needed
    so every job is reachable regardless of match/docs state."""
    from app.models.application import Application
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    app_obj = job.applications[0] if job.applications else None
    if not app_obj:
        app_obj = Application(job_id=job.id)
        db.add(app_obj)
        db.commit()
        db.refresh(app_obj)
    return RedirectResponse(url=f"/apps/{app_obj.id}", status_code=302)


@router.post("/{job_id}/override", response_class=HTMLResponse)
def override_job_status(job_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status == JobStatus.matched:
        job.status = JobStatus.filtered_out
        job.filter_reason = "manual"
        job.filter_detail = "You filtered this out from the jobs list."
    elif job.status == JobStatus.filtered_out:
        job.status = JobStatus.matched
        # Reinstated by hand — the old explanation no longer applies.
        job.filter_reason = None
        job.filter_detail = None
    db.commit()
    return templates.TemplateResponse(
        "jobs/partials/job_card.html",
        {"request": request, "job": job},
    )
