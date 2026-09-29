import re
import uuid
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse
from app.templating import build as build_templates
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.database import get_db
from app.models.application import Application
from app.models.job import Job, JobStatus
from app.services.locations import REGIONS, REGION_OPTIONS, resolve_region_key
from app.services.matcher import FILTER_REASON_LABELS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["jobs"])
templates = build_templates()


def _dismiss_reasons() -> list[tuple[str, str]]:
    """The reasons a card offers for "Not interested"; the company has its own button."""
    from app.services.match_report import DISMISS_REASONS

    return [(key, label) for key, (label, _) in DISMISS_REASONS.items() if key != "company"]


templates.env.globals["dismiss_reasons"] = _dismiss_reasons()

# `new` belongs here: a freshly fetched job is real and worth seeing before the
# matcher has had its say. Leaving it out made every job invisible until a
# matching cycle ran — and a source-scoped manual fetch skips matching entirely,
# so those jobs would never have appeared at all.
_FILTERABLE_STATUSES = [
    JobStatus.new, JobStatus.matched, JobStatus.filtered_out,
    JobStatus.docs_generated,
]
_PAGE_SIZE = 50
_EXP_LEVELS = ["entry", "mid", "senior"]


def _known_sources(db: Session) -> list[str]:
    """
    Sources that actually have jobs.

    The previous hand-kept list had drifted and omitted a third of the adapters
    (jooble, careerjet, themuse, jobicy, the ATS boards…), so those couldn't be
    filtered to at all.
    """
    rows = db.query(Job.source).distinct().order_by(Job.source).all()
    return [r[0] for r in rows if r[0]]


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


# Not every source reports a posting date, and the card falls back to the
# fetched date when one is missing. Sorting on `posted_at` alone therefore
# ordered by a value the reader couldn't see, dumping every dateless job at the
# bottom under a recent-looking date. Sort on the same expression we display.
_EFFECTIVE_DATE = func.coalesce(Job.posted_at, Job.fetched_at)

# The score that decided the job's fate: the second opinion where one was
# taken, the first otherwise. Mirrors Job.effective_score, in SQL.
_EFFECTIVE_SCORE = func.coalesce(Job.llm_score_deep, Job.llm_score)


def _favourite_count(db: Session) -> int:
    """
    How many jobs are starred.

    Counted without the status filter the rest of this page uses: a favourite
    the matcher later filtered out is still on the shortlist, and a number that
    disagreed with what the favourites view shows would be worse than none.
    """
    return (
        db.query(func.count(Job.id)).filter(Job.favourite.is_(True)).scalar()
    ) or 0


def _undated_count(db: Session) -> int:
    """How many visible jobs never reported a posting date."""
    return (
        db.query(func.count(Job.id))
        .filter(Job.status.in_(_FILTERABLE_STATUSES), Job.posted_at.is_(None))
        .scalar()
    ) or 0


def _age_cutoff(db: Session) -> datetime | None:
    """
    The oldest `fetched_at` the list shows by default, or None for no limit.

    Read through `tunables.value` rather than off `settings` directly, because
    the setting is editable from the settings page and that stores the override
    on the profile blob. Reading the environment value here would have left the
    control in the UI doing nothing — which is the shape of the bug three
    source adapters already have with `MAX_JOB_AGE_DAYS`.

    Still defensive about the result: `coerce` falls back to the environment
    value when a stored one is unreadable, and that can be anything a `.env`
    holds. A bad value should widen the list rather than empty it.
    """
    from app.models.profile import Profile
    from app.services import tunables

    profile = db.query(Profile).first()
    try:
        days = int(tunables.value(profile.data if profile else None,
                                  "dashboard_max_age_days"))
    except (TypeError, ValueError, KeyError):
        return None
    if days <= 0:
        return None
    return datetime.now(timezone.utc) - timedelta(days=days)


def _recent_or_mine(cutoff: datetime):
    """
    Fresh enough to be worth looking at, or yours regardless of age.

    The two exemptions are the same ones `services.archive` makes, for the same
    reason: an application means the row is the user's pipeline and not a
    listing, and a star is them saying so explicitly. A six-week-old job you
    applied to disappearing from the list would be a bug, not a feature.

    An explicit EXISTS rather than `Job.applications.any()`, because that
    relationship is a backref and so does not exist as an attribute until
    SQLAlchemy has configured its mappers. Tests that drive this router with a
    mock session never trigger that configuration, and the first version of
    this failed on them with `type object 'Job' has no attribute
    'applications'` — a real fragility rather than a test artefact, since it
    means the query depends on whatever ran before it.
    """
    applied = (
        select(Application.id)
        .where(Application.job_id == Job.id)
        .exists()
    )
    return or_(
        Job.fetched_at >= cutoff,
        Job.favourite.is_(True),
        applied,
    )


def _stale_count(db: Session, cutoff: datetime | None) -> int:
    """
    How many jobs the age cutoff is holding back.

    Reported on the page for the same reason `_priced_count` is: a filter that
    silently removes rows reads as a broken list, and the only cure is for the
    page to say how many it is hiding and offer the switch to see them.

    Coerced to an int inside a guard for that same reason. The template formats
    this with a thousands separator, so anything that is not a number takes the
    whole page down with a `TypeError` — a worse outcome than a missing count,
    and the exact failure `_priced_count` below was already written to avoid.
    """
    if cutoff is None:
        return 0
    try:
        return int(
            db.query(func.count(Job.id))
            .filter(
                Job.status.in_(_FILTERABLE_STATUSES),
                ~_recent_or_mine(cutoff),
            )
            .scalar()
            or 0
        )
    except (TypeError, ValueError):
        return 0


def _priced_count(db: Session) -> int:
    """
    How many visible jobs state any pay at all.

    Shown beside the salary filter because most postings don't, and a filter
    that silently hides 90% of the list reads as a broken filter unless the
    page says up front how many jobs it can possibly match.
    """
    try:
        return int(
            db.query(func.count(Job.id))
            .filter(
                Job.status.in_(_FILTERABLE_STATUSES),
                # Counted on the annualised pair, because that is what the
                # filter beside this label actually compares — a count of rows
                # the filter cannot match would be worse than no count.
                func.coalesce(Job.salary_annual_max, Job.salary_annual_min).isnot(None),
            )
            .scalar()
            or 0
        )
    except (TypeError, ValueError):
        # A count the page cannot render is worse than no count: this label is
        # a hint beside a filter, not the page's reason for existing.
        return 0


# How many days of age cost a point of score in the "score and freshness"
# sort, up to this many: a posting a month old has usually had its applicants.
_FRESHNESS_DAYS = 30
_AGE_DAYS = func.extract("epoch", func.now() - _EFFECTIVE_DATE) / 86400.0

_SORT_OPTIONS = {
    # Sort and filter on the score the card actually shows. Ranking by the
    # first pass while displaying the second would put an 82 above a 91 with
    # no visible reason.
    "score_desc": (_EFFECTIVE_SCORE.desc().nullslast(),),
    "score_asc": (_EFFECTIVE_SCORE.asc().nullsfirst(),),
    # A point off per day of age, for a month: an 84 posted today above an
    # 88 posted three weeks ago, which is the order they are worth reading in.
    "fresh_desc": ((_EFFECTIVE_SCORE - func.least(_AGE_DAYS, _FRESHNESS_DAYS)).desc().nullslast(),),
    "posted_desc": (_EFFECTIVE_DATE.desc(),),
    "posted_asc": (_EFFECTIVE_DATE.asc(),),
    # On the annualised band, like the salary filter, with unpriced jobs last.
    "salary_desc": (func.coalesce(Job.salary_annual_max, Job.salary_annual_min).desc().nullslast(),
                    _EFFECTIVE_SCORE.desc().nullslast()),
    # The skills the matcher said you lack, fewest first; an unscored job has
    # no list rather than an empty one, so it goes last.
    "missing_asc": (case((_EFFECTIVE_SCORE.is_(None), 999),
                         else_=func.coalesce(func.cardinality(Job.missing_skills), 0)).asc(),
                    _EFFECTIVE_SCORE.desc().nullslast()),
    "company_asc": (Job.company.asc(),),
    # The useful order on the favourites view, and the default there. Sorting a
    # shortlist by score would bury the job starred this morning under one
    # starred last month that happened to score higher.
    "favourited_desc": (Job.favourited_at.desc().nullslast(),),
}

# Every filter the list takes. A saved view is a query string of these, so it
# can be counted without a request.
FILTER_PARAMS = (
    "status", "q", "q_in", "source", "region", "location", "remote", "min_score",
    "min_salary", "exp_level", "filter_reason", "dated", "favourite", "age", "sponsor",
    "h1b", "open_only", "employment_type", "max_years", "applied", "company", "sort",
)

_APPLIED_STATUSES = ("applied", "interviewing", "offered", "rejected", "withdrawn")


def _applied_clause():
    """Jobs with an application past "not applied"."""
    from app.models.application import Application, ApplicationStatus

    statuses = [ApplicationStatus(s) for s in _APPLIED_STATUSES
                if s in ApplicationStatus.__members__]
    return (select(Application.id)
            .where(Application.job_id == Job.id, Application.status.in_(statuses))
            .exists())


def _h1b_companies(db: Session, query) -> list[str] | None:
    """
    The employers in `query` with certified H-1B filings, or None with no data.

    Matched in Python by `sponsorship_history.for_company`, the same reading
    the card's H-1B line uses: its name matching (suffixes, "Amazon" for
    "Amazon.com Services") has no SQL equivalent, and the distinct employers
    on the list are a few thousand at most.
    """
    from app.services.sponsorship_history import for_company, snapshot

    if not snapshot(db):
        return None
    names = [c for (c,) in query.with_entities(Job.company).distinct().limit(20000) if c]
    return [n for n in names if ((for_company(n, db) or {}).get("certified") or 0) > 0]


def filtered(db: Session, params: dict) -> tuple:
    """
    The list's query for these parameters, the sort to use, and notes.

    `params` is a dict of the strings in FILTER_PARAMS, as a request or a
    saved view carries them; missing means unset.
    """
    get = lambda key: (params.get(key) or "").strip()  # noqa: E731
    sort = get("sort") or "score_desc"
    notes: dict = {}
    query = db.query(Job).filter(Job.status.in_(_FILTERABLE_STATUSES))

    # Old listings are noise, so the list looks back DASHBOARD_MAX_AGE_DAYS by
    # default. `?age=all` lifts it; anything the user applied to or starred is
    # exempt either way — see `_recent_or_mine`.
    cutoff = _age_cutoff(db)
    if cutoff is not None and get("age") != "all":
        query = query.filter(_recent_or_mine(cutoff))

    # Checked before anything else so the shortlist is the shortlist: a starred
    # job that the matcher filtered out must still appear here, and a status or
    # score filter carried over from the previous view would hide the very rows
    # this page exists to show.
    if get("favourite") == "1":
        query = query.filter(Job.favourite.is_(True))
        if sort == "score_desc":
            sort = "favourited_desc"

    status, q = get("status"), get("q")
    if status:
        try:
            query = query.filter(Job.status == JobStatus(status))
        except ValueError:
            pass
    if q:
        pattern = f"%{q}%"
        clause = Job.title.ilike(pattern) | Job.company.ilike(pattern)
        # The text too, when asked: slower (no index covers it) but the only
        # way to find the postings that mention a tool in their body.
        if get("q_in") == "all":
            clause = (clause | Job.description.ilike(pattern)
                      | func.array_to_string(Job.required_skills, " ").ilike(pattern)
                      | func.array_to_string(Job.nice_to_have_skills, " ").ilike(pattern))
        query = query.filter(clause)
    if get("company"):
        query = query.filter(Job.company.ilike(f"%{get('company')}%"))
    if get("source"):
        query = query.filter(Job.source == get("source"))
    region, location = get("region"), get("location")
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
    if get("remote") == "1":
        query = query.filter(Job.is_remote == True)  # noqa: E712
    if get("exp_level"):
        query = query.filter(Job.experience_level == get("exp_level"))
    if get("employment_type"):
        query = query.filter(Job.employment_type == get("employment_type"))
    if get("min_score"):
        try:
            query = query.filter(_EFFECTIVE_SCORE >= int(get("min_score")))
        except ValueError:
            pass
    if get("max_years"):
        try:
            most = float(get("max_years"))
        except ValueError:
            most = None
        if most is not None:
            # A posting that states no years is not one that asks too many.
            query = query.filter(or_(Job.required_years.is_(None), Job.required_years <= most))
    if get("min_salary"):
        try:
            floor = float(get("min_salary"))
        except ValueError:
            floor = None
        if floor is not None:
            # Against the top of the band, not the bottom: "$120k–$180k" clears
            # a $150k floor, and filtering on the minimum would hide it. Jobs
            # that state no salary are excluded rather than assumed to pay
            # nothing — but that is most of them, so the UI says so.
            #
            # And against the *annualised* band, which is the whole point of
            # having one. The stated figures are whatever the posting wrote:
            # comparing them to a floor put an hourly rate, a monthly rate and
            # a euro figure on the same axis, so a $100k floor hid a $65/hour
            # posting worth about $135k and admitted a €100,000 one. A row we
            # could not annualise honestly — no stated period, or a currency
            # with no rate — has NULL here and is excluded, which is the same
            # treatment a row stating no pay at all already got.
            query = query.filter(
                func.coalesce(Job.salary_annual_max, Job.salary_annual_min) >= floor
            )
    # What the posting says about sponsoring a visa: "not_no" drops the ones
    # that say they will not, "yes" keeps only the ones that say they will.
    if get("sponsor") == "not_no":
        query = query.filter(or_(Job.sponsorship_direction.is_(None),
                                 Job.sponsorship_direction != "negative"))
    elif get("sponsor") == "yes":
        query = query.filter(Job.sponsorship_direction == "positive")
    if get("h1b") == "1":
        companies = _h1b_companies(db, query)
        if companies is None:
            notes["h1b_unavailable"] = True
        else:
            query = query.filter(Job.company.in_(companies or [""]))
    if get("open_only") == "1":
        query = query.filter(Job.closed_at.is_(None))
    if get("applied") == "yes":
        query = query.filter(_applied_clause())
    elif get("applied") == "no":
        query = query.filter(~_applied_clause())
    if get("filter_reason"):
        query = query.filter(Job.filter_reason == get("filter_reason"))
    # A job with no posting date skips the fetcher's age check entirely, so
    # some of these are long-closed listings passing as fresh. Being able to
    # see them as a group is the difference between suspecting that and knowing.
    if get("dated") == "1":
        query = query.filter(Job.posted_at.isnot(None))
    elif get("dated") == "0":
        query = query.filter(Job.posted_at.is_(None))
    return query, sort, cutoff, notes


def _employment_types(db: Session) -> list[str]:
    try:
        rows = db.query(Job.employment_type).filter(Job.employment_type.isnot(None)).distinct().all()
        return sorted(r[0] for r in rows if r[0])
    except Exception:
        return []


@router.get("", response_class=HTMLResponse)
def get_jobs(request: Request, page: int = 0, view: str = "", db: Session = Depends(get_db)):
    from app.services import for_you, job_views

    params = {key: request.query_params.get(key, "") for key in FILTER_PARAMS}
    profile_data = _profile_data(db)
    views = job_views.views(profile_data)

    # The default view opens the page, unless the page was asked for with
    # filters of its own or told to show everything (`view=none`).
    default = job_views.default(views)
    if default and not view and not any(params.values()) and not page:
        return RedirectResponse(url=f"/jobs?{default['query']}&view={default['id']}",
                                status_code=303)

    query, sort, cutoff, notes = filtered(db, params)
    ranking = for_you.model_for(profile_data)
    if sort == "for_you" and ranking is None:
        sort = "score_desc"
    total = query.count()
    if sort == "for_you":
        jobs = _ranked_page(db, query, ranking, page)
    else:
        order = _SORT_OPTIONS.get(sort, _SORT_OPTIONS["score_desc"])
        # Asked for here rather than declared on the relationship. Every card
        # renders its score history, so without this the page is fifty
        # queries — but as a relationship-level `selectin` it was also loaded
        # by every batch pass that touches a Job, which is thousands of rows of
        # nobody's business.
        jobs = (
            query.options(selectinload(Job.scores))
            .order_by(*order)
            .offset(page * _PAGE_SIZE)
            .limit(_PAGE_SIZE)
            .all()
        )

    current_query = job_views.query_string(params)
    return templates.TemplateResponse(
        "jobs/index.html",
        {
            "request": request,
            "jobs": jobs,
            "filter_reason_filter": params["filter_reason"],
            "filter_reason_counts": _filter_reason_counts(db),
            "filter_reason_labels": FILTER_REASON_LABELS,
            "status_filter": params["status"],
            "q": params["q"],
            "q_in": params["q_in"],
            "source_filter": params["source"],
            "region_filter": params["region"],
            "location_filter": params["location"],
            "remote_filter": params["remote"],
            "min_score_filter": params["min_score"],
            "min_salary_filter": params["min_salary"],
            "priced_count": _priced_count(db),
            "exp_level_filter": params["exp_level"],
            "dated_filter": params["dated"],
            "undated_count": _undated_count(db),
            "favourite_filter": params["favourite"],
            "favourite_count": _favourite_count(db),
            "age_filter": params["age"],
            "sponsor_filter": params["sponsor"],
            "h1b_filter": params["h1b"],
            "open_only_filter": params["open_only"],
            "employment_type_filter": params["employment_type"],
            "max_years_filter": params["max_years"],
            "applied_filter": params["applied"],
            "company_filter": params["company"],
            "employment_types": _employment_types(db),
            "notes": notes,
            # Derived from the cutoff rather than read again, so the number
            # in the banner cannot disagree with the filter that produced it.
            "age_days": (
                (datetime.now(timezone.utc) - cutoff).days
                if cutoff is not None else 0
            ),
            "stale_count": _stale_count(db, cutoff),
            "sort": sort,
            "page": page,
            "total": total,
            "page_size": _PAGE_SIZE,
            "has_prev": page > 0,
            "has_next": (page + 1) * _PAGE_SIZE < total,
            "sources": _known_sources(db),
            "exp_levels": _EXP_LEVELS,
            "region_options": REGION_OPTIONS,
            "for_you_available": ranking is not None,
            "views": job_views.with_counts(
                views, current_query, lambda saved: filtered(db, saved)[0].count()),
            "current_query": current_query,
            "active_view": view,
        },
    )


# What the "For you" ranking reads, loaded for every row the filters keep —
# no descriptions, which are most of a job row's bytes.
_RANKING_COLUMNS = (
    Job.id, Job.title, Job.company, Job.source, Job.experience_level, Job.llm_score,
    Job.llm_score_deep, Job.similarity, Job.keyword_score, Job.is_remote,
    Job.salary_annual_max, Job.salary_annual_min, Job.posted_at, Job.fetched_at,
    Job.required_years,
)


def _profile_data(db: Session) -> dict:
    from app.models.profile import Profile

    try:
        profile = db.query(Profile).first()
    except Exception:
        return {}
    return (profile.data if profile is not None and isinstance(profile.data, dict) else {})


def _ranked_page(db: Session, query, ranking: dict, page: int) -> list:
    """One page of the filtered jobs in "For you" order."""
    from app.services import for_you

    ranked = for_you.rank(ranking, query.with_entities(*_RANKING_COLUMNS).all())
    ids = [row.id for _, row in ranked[page * _PAGE_SIZE:(page + 1) * _PAGE_SIZE]]
    if not ids:
        return []
    by_id = {job.id: job for job in
             db.query(Job).options(selectinload(Job.scores)).filter(Job.id.in_(ids)).all()}
    return [by_id[i] for i in ids if i in by_id]


@router.post("/views")
def save_view(name: str = Form(""), query: str = Form(""), db: Session = Depends(get_db)):
    """Save the list as it is now under a name."""
    from app.services import job_views

    try:
        view = job_views.save(db, name, query)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return RedirectResponse(url=f"/jobs?{view['query']}&view={view['id']}", status_code=303)


@router.post("/views/{view_id}/default")
def default_view(view_id: str, db: Session = Depends(get_db)):
    """Open /jobs on this view, or stop doing so."""
    from app.services import job_views

    try:
        job_views.set_default(db, view_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="No such view")
    view = next(v for v in job_views.views(_profile_data(db)) if v["id"] == view_id)
    return RedirectResponse(url=f"/jobs?{view['query']}&view={view_id}", status_code=303)


@router.post("/views/{view_id}/delete")
def delete_view(view_id: str, db: Session = Depends(get_db)):
    from app.services import job_views

    job_views.remove(db, view_id)
    return RedirectResponse(url="/jobs?view=none", status_code=303)


BULK_ACTIONS = ("star", "unstar", "hide", "restore", "generate")


@router.post("/bulk", response_class=HTMLResponse)
def bulk_action(
    job_ids: list[str] = Form(default=[]),
    action: str = Form(...),
    reason: str = Form(""),
    db: Session = Depends(get_db),
):
    """
    One action on every selected job, then reload the list.

    Each does what the card's own button does, job by job: a star, "Not
    interested" (with its reason), putting a dismissed job back, queueing
    documents. Jobs an action does not apply to (documents for a job the
    matcher filtered out) are skipped and counted.
    """
    from app.services.match_report import DISMISS_REASONS
    from app.tasks.generate import (
        NEEDS_GENERATION, claim_for_generation, queue_generation, release_generation_claim)

    if action not in BULK_ACTIONS:
        raise HTTPException(status_code=422, detail=f"Unknown action: {action}")
    if reason and reason not in DISMISS_REASONS:
        raise HTTPException(status_code=422, detail=f"Unknown reason: {reason}")
    ids = []
    for raw in job_ids:
        try:
            ids.append(uuid.UUID(raw))
        except ValueError:
            continue
    jobs = db.query(Job).filter(Job.id.in_(ids)).all() if ids else []
    now = datetime.now(timezone.utc)
    done = skipped = 0
    for job in jobs:
        if action in ("star", "unstar"):
            job.favourite = action == "star"
            job.favourited_at = now if job.favourite else None
        elif action == "hide":
            job.status = JobStatus.filtered_out
            job.filter_reason = "manual"
            job.filter_detail = "You filtered this out from the jobs list."
            job.dismiss_reason = reason or None
            job.dismissed_at = now
        elif action == "restore":
            if job.status != JobStatus.filtered_out or job.filter_reason != "manual":
                skipped += 1
                continue
            job.status = JobStatus.matched
            job.filter_reason = job.filter_detail = job.dismiss_reason = None
            job.dismissed_at = None
        elif action == "generate":
            app_obj = job.applications[0] if job.applications else None
            # Only where nothing is written or running: a bulk click is not a
            # reason to spend six model calls rewriting documents that exist.
            if (job.status != JobStatus.matched or app_obj is None
                    or not claim_for_generation(db, app_obj.id, NEEDS_GENERATION)):
                skipped += 1
                continue
            if not queue_generation(app_obj.id):
                release_generation_claim(db, app_obj.id)
                skipped += 1
                continue
        done += 1
    db.commit()
    logger.info("jobs bulk %s: %d done, %d skipped", action, done, skipped)
    return HTMLResponse(
        f'<span class="text-xs">{done} done{f", {skipped} skipped" if skipped else ""}</span>',
        headers={"HX-Refresh": "true"},
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


def _add_to_profile_list(db: Session, key: str, value: str) -> None:
    """
    Append one value to a list on the profile blob, if it isn't there already.

    A fresh read right before the write keeps the window against concurrent
    profile writers (the fetch cycle merges only its own keys) small.
    """
    import copy

    from app.models.profile import Profile

    profile = db.query(Profile).first()
    if profile is None:
        return
    data = copy.deepcopy(profile.data or {})
    existing = [str(v) for v in (data.get(key) or [])]
    if value.lower() not in {v.lower() for v in existing}:
        data[key] = existing + [value]
        profile.data = data


@router.post("/{job_id}/not-interested", response_class=HTMLResponse)
def not_interested(
    job_id: uuid.UUID,
    request: Request,
    scope: str = Form(...),
    word: str = Form(""),
    reason: str = Form(""),
    db: Session = Depends(get_db),
):
    """
    Filter this job out, and — when asked — learn from it.

    The scope is the user's explicit choice, never inferred: "job" hides only
    this posting, "company" also excludes the employer from future matching,
    and "title_word" blocks a word they picked off this title. Every option
    used to be a correction the system threw away.

    `reason` (a key of match_report.DISMISS_REASONS) says why, when the user
    picked one; excluding a company or a title word is its own reason. Kept
    with the time, for the matching report and the "For you" ranking.
    """
    from app.services.match_report import DISMISS_REASONS

    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if reason and reason not in DISMISS_REASONS:
        raise HTTPException(status_code=422, detail=f"Unknown reason: {reason}")
    job.dismiss_reason = reason or {"company": "company", "title_word": "role"}.get(scope)
    job.dismissed_at = datetime.now(timezone.utc)

    if scope == "company":
        company = (job.company or "").strip()
        if not company:
            raise HTTPException(status_code=422, detail="This job has no company to exclude")
        _add_to_profile_list(db, "excluded_companies", company)
        job.filter_reason = "excluded_company"
        job.filter_detail = f"You excluded {company}; future postings from them are filtered too."
    elif scope == "title_word":
        chosen = (word or "").strip()
        # Only a word actually present in this title: the button list is the
        # interface, and a free-typed stray would silently block half the feed.
        if not chosen or not re.search(
            rf"\b{re.escape(chosen)}\b", job.title or "", re.IGNORECASE
        ):
            raise HTTPException(status_code=422, detail="Pick a word from this job's title")
        _add_to_profile_list(db, "blocked_title_words", chosen)
        job.filter_reason = "blocked_title"
        job.filter_detail = (
            f"You blocked {chosen!r}; titles containing it are filtered from now on."
        )
    elif scope == "job":
        job.filter_reason = "manual"
        job.filter_detail = "You filtered this out from the jobs list."
    else:
        raise HTTPException(status_code=422, detail=f"Unknown scope: {scope}")

    job.status = JobStatus.filtered_out
    db.commit()
    return templates.TemplateResponse(
        "jobs/partials/job_card.html",
        {"request": request, "job": job},
    )


@router.post("/{job_id}/rematch", response_class=HTMLResponse)
def rematch_job(job_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    """
    Score this job again, now, and show the result.

    Enrichment re-queues jobs automatically when their description grows, but
    only for verdicts it reached by reading one — and never for a job you
    already have an application for. This is the manual door: a posting you
    think was judged wrongly, re-read against the profile as it stands today.

    Synchronous rather than queued. It is one LLM call on an explicit click,
    and the whole point is to see the new score — "queued, come back later"
    for a button you pressed because you disagreed with a number is barely
    better than not having the button.

    Everything else comes for free by calling the real `match_job`: the verdict
    lands in the score history beside the old one, the second-opinion pass runs
    if the score is a close call, and structured details are extracted if they
    were never read. A path of its own would have had to remember all three.

    A normal re-score is a few seconds. The pathological one — a provider
    rate-limiting us into the retry loop — can outlast Caddy's 60-second read
    timeout, and the browser then shows an error. That is untidy rather than
    harmful: the commit below happens on the server either way, so the new
    score is there on the next page load.
    """
    from app.models.profile import Profile
    from app.services.matcher import match_job
    from app.services.tunables import value as tunable

    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    profile = db.query(Profile).first()
    profile_data = (profile.data if profile else None) or {}

    try:
        outcome = match_job(
            db, job, profile_data,
            settings.NVIDIA_NIM_API_KEY, settings.NVIDIA_NIM_BASE_URL,
            tunable(profile_data, "nvidia_nim_model"),
        )
        db.commit()
    except Exception as exc:
        # The job keeps whatever it had. A failed re-score must not leave it
        # worse off than not pressing the button.
        db.rollback()
        logger.error("rematch: scoring job %s failed: %s", job_id, exc)
        raise HTTPException(
            status_code=502,
            detail=f"Could not score this job right now: {exc}",
        ) from exc

    if outcome == "rate_limited":
        # `match_job` leaves the job `new` so the scheduled pass retries it.
        # Saying so beats a card that looks unchanged for no visible reason.
        raise HTTPException(
            status_code=503,
            detail="Every model provider refused that call. The job is queued "
                   "for the next scheduled matching pass.",
        )

    logger.info("rematch: job %s re-scored by hand — %s", job_id, outcome)
    return templates.TemplateResponse(
        "jobs/partials/job_card.html",
        {"request": request, "job": job},
    )


@router.post("/{job_id}/favourite", response_class=HTMLResponse)
def toggle_favourite(job_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    """
    Star or unstar a job.

    Deliberately touches nothing but the two favourite columns. Starring a job
    the matcher filtered out is a common and meaningful thing to do — it is the
    clearest disagreement with a verdict there is — and silently re-opening it
    would turn a bookmark into an override the user did not ask for. The one
    consequence is that a favourite is never archived.
    """
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    job.favourite = not job.favourite
    job.favourited_at = datetime.now(timezone.utc) if job.favourite else None
    db.commit()

    return templates.TemplateResponse(
        "jobs/partials/job_card.html",
        {"request": request, "job": job},
    )


def _safe_next(raw: str, fallback: str) -> str:
    """
    Where to go after saving, when the caller said.

    Only a path on this app: `next` arrives in a query string, and an absolute
    URL there is an open redirect waiting to be pasted into a message. There is
    one user here and nobody to phish, but a redirect that can leave the site
    is also just wrong — the button says "back to the job".
    """
    target = (raw or "").strip()
    if target.startswith("/") and not target.startswith("//"):
        return target
    return fallback


@router.get("/{job_id}/edit", response_class=HTMLResponse)
def edit_job_form(
    job_id: uuid.UUID,
    request: Request,
    next: str = "",
    db: Session = Depends(get_db),
):
    """The form for correcting a job by hand."""
    from app.services import job_edits

    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return templates.TemplateResponse(
        "jobs/edit.html",
        {
            "request": request,
            "job": job,
            "fields": job_edits.EDITABLE,
            "locked": job_edits.locked(job),
            "next": _safe_next(next, f"/jobs/{job.id}/application"),
            "errors": {},
        },
    )


@router.post("/{job_id}/edit", response_class=HTMLResponse)
async def save_job_edit(
    job_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Store hand-edited fields, and lock them against everything automatic.

    The whole form is read from the raw body rather than declared as `Form(...)`
    parameters: the editable set lives in `job_edits.EDITABLE` and adding a
    field there should not also require a signature change here.

    Unchecked checkboxes are absent from a form post, which for `is_remote`
    means "off" rather than "leave alone" — so the form carries a marker naming
    every checkbox it rendered, and the missing ones are filled in as false.
    """
    form = await request.form()
    return await run_in_threadpool(_save_job_edit, job_id, request, db, form)


def _save_job_edit(job_id, request, db, form):
    from app.services import job_edits

    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    values = {
        field: form[field]
        for field in job_edits.EDITABLE
        if field in form
    }
    for field in form.getlist("_checkbox"):
        values.setdefault(field, "")

    destination = _safe_next(str(form.get("next") or ""), f"/jobs/{job.id}/application")

    try:
        outcome = job_edits.apply(
            db, job, values, release_fields=form.getlist("release")
        )
        db.commit()
    except job_edits.EditError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "jobs/edit.html",
            {
                "request": request, "job": job,
                "fields": job_edits.EDITABLE,
                "locked": job_edits.locked(job),
                "next": destination,
                "errors": {"form": str(exc)},
            },
            status_code=422,
        )
    except Exception as exc:
        db.rollback()
        logger.error("edit: saving job %s failed: %s", job_id, exc)
        raise HTTPException(status_code=502, detail=f"Could not save: {exc}") from exc

    logger.info(
        "edit: job %s — changed %s, released %s",
        job_id, outcome["changed"] or "nothing", outcome["released"] or "nothing",
    )
    return RedirectResponse(url=destination, status_code=303)


@router.post("/{job_id}/override", response_class=HTMLResponse)
def override_job_status(job_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status == JobStatus.matched:
        job.status = JobStatus.filtered_out
        job.filter_reason = "manual"
        job.filter_detail = "You filtered this out from the jobs list."
        job.dismissed_at = datetime.now(timezone.utc)
    elif job.status == JobStatus.filtered_out:
        job.status = JobStatus.matched
        # Reinstated by hand — the old explanation no longer applies, and the
        # dismissal it undoes is no longer a "no".
        job.filter_reason = None
        job.filter_detail = None
        job.dismiss_reason = None
        job.dismissed_at = None
    db.commit()
    return templates.TemplateResponse(
        "jobs/partials/job_card.html",
        {"request": request, "job": job},
    )
