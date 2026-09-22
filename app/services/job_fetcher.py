import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import settings
from app.models.job import Job, JobStatus
from app.models.profile import Profile
from app.services.deduplication import (
    compute_dedupe_hash, enrich_from, find_existing_job, merge_description,
    merge_or_skip, was_archived,
)
from app.services.descriptions import clean as clean_description

logger = logging.getLogger(__name__)

# Give the one-time board backfill a few shots at a flaky network, then stop.
_MAX_BACKFILL_ATTEMPTS = 3

# Rows to insert before committing. A cycle used to hold every insert in one
# transaction and commit once at the end, so a single failure there discarded
# the whole cycle's work — minutes of requests, thrown away with a log line.
_COMMIT_EVERY = 250

# The profile-blob keys a fetch cycle owns. Only these are written back at the
# end of a cycle; everything else on the blob belongs to other writers (agent
# presence, mailbox state, settings) and must survive a cycle that overlaps
# them. Cycles themselves are serialized by the fetch lock, so two cycles never
# race each other on these.
_FETCH_CYCLE_KEYS = (
    "search_query_cache", "ats_slug_cache", "ats_slug_report",
    "discovered_ats", "ats_sniff_cache", "last_fetch",
)

# Structured fields an adapter may already have been handed, and which would
# otherwise be thrown away and re-derived from prose by an LLM call later.
# USAJOBS states pay on every posting; so does any board publishing JobPosting
# structured data. Only filled when the adapter supplies them — a source that
# says nothing leaves the column null, which is what null means here.
_ADAPTER_DETAIL_FIELDS = (
    "salary_min", "salary_max", "salary_currency", "employment_type",
    # What the figures are per. Without it the band cannot be annualised, and
    # an un-annualised band is invisible to the salary floor — so a posting
    # that states its pay would be missing from a pay filter.
    "salary_period",
)


def _adapter_details(job_data: dict) -> dict:
    details = {
        field: job_data.get(field)
        for field in _ADAPTER_DETAIL_FIELDS
        if job_data.get(field) is not None
    }
    # A currency or a period without an amount says nothing, and would read as
    # a stated salary to the "does this job state pay?" count.
    if details.get("salary_min") is None and details.get("salary_max") is None:
        details.pop("salary_currency", None)
        details.pop("salary_period", None)
        return details
    # Derived here rather than left to a later enrichment pass, for the reason
    # `job_details.with_annual` gives: the filter reads the annual columns.
    from app.services.job_details import with_annual

    return with_annual(details)


# The pipeline in three slices, so each can run on the schedule it deserves.
#
# One task used to fetch everything, and it took 47 minutes: Adzuna — an API
# call that could refresh hourly — waited behind a Chromium launch that only
# needs to happen twice a day, and every posting arrived hours later than it
# could have. Splitting them costs a little duplicated setup per run and buys
# API postings within the hour.
SOURCE_GROUPS: dict[str, frozenset[str]] = {
    # Cheap keyed/public APIs and feeds. Minutes, not tens of minutes.
    "api": frozenset({
        "adzuna", "jsearch", "jooble", "careerjet", "findwork", "usajobs",
        "hiringcafe", "ycombinator", "linkedin", "indeed", "remotive",
        "arbeitnow", "remoteok", "weworkremotely", "themuse", "himalayas",
        "jobicy", "hnhiring", "workingnomads", "builtin", "jobspresso",
    }),
    # The company board registry: hundreds of slugs, one request each.
    "boards": frozenset({
        "greenhouse", "lever", "ashby", "smartrecruiters", "workable",
        "recruitee", "workday", "icims", "bamboohr", "teamtailor", "jobvite",
        "personio",
    }),
    # Playwright. The expensive tier, and the one worth running least often.
    "browser": frozenset({"wellfound", "dice", "handshake"}),
}

ALL_GROUPS = tuple(SOURCE_GROUPS)


def group_sources(group: str | None) -> set[str] | None:
    """The sources one group covers; None for a run of everything."""
    if not group or group == "all":
        return None
    try:
        return set(SOURCE_GROUPS[group])
    except KeyError:
        raise ValueError(
            f"Unknown fetch group {group!r}. Known groups: "
            f"{', '.join(ALL_GROUPS)}"
        ) from None


class _BrowserTierSkipped(Exception):
    """Internal signal: no Playwright source was requested this run."""


def _reset_source_caches() -> None:
    """Clear per-cycle adapter caches so each cycle starts from live data."""
    from app.services.sources import arbeitnow, wellfound

    for module in (arbeitnow, wellfound):
        try:
            module.reset_cache()
        except Exception as exc:  # never let a cache reset break a fetch
            logger.warning("could not reset %s cache: %s", module.__name__, exc)


def _get_slugs(raw: str) -> list[str]:
    return [s.strip() for s in raw.split(",") if s.strip()]


def _record(stats: dict, source: str, jobs: list[dict], error: str | None = None) -> None:
    """Accumulate per-source fetch stats."""
    entry = stats.setdefault(source, {"count": 0, "errors": []})
    entry["count"] += len(jobs)
    if error:
        entry["errors"].append(error)


def _run_combos(
    stats: dict, all_jobs: list, source: str, fetch_one, combos, skip=None,
) -> None:
    """
    Call `fetch_one(*combo)` over each combination, recording per-source stats.

    Stops the whole source the moment it raises SourceUnavailable — a rejected
    key or a spent quota answers the same way for every remaining query, and
    walking them all just produced dozens of identical errors (and, for
    rate-limited APIs, made the situation worse).
    """
    from app.services.sources.base import SourceUnavailable

    if skip is not None and skip(source):
        return
    stats.setdefault(source, {"count": 0, "errors": [], "enabled": True})
    for combo in combos:
        try:
            jobs = fetch_one(*combo)
        except SourceUnavailable as exc:
            _record(stats, source, [], str(exc))
            logger.warning("%s: %s", source, exc)
            return
        except Exception as exc:
            label = "/".join(str(c) for c in combo)
            _record(stats, source, [], f"{label}: {exc}")
            continue
        _record(stats, source, jobs)
        all_jobs.extend(jobs)


def _run_all_adapters(
    roles: list[str], locations: list[str], cfg,
    ats_slugs: dict | None = None, loc_prefs: dict | None = None,
    only: set[str] | None = None, resting: dict | None = None,
    manual: bool | None = None,
) -> tuple[list[dict], dict]:
    """
    Call all enabled adapters and return (all_jobs, source_stats).
    source_stats: {source: {"count": N, "errors": [...], "enabled": bool}}
    ats_slugs: final slug list per ATS (configured + seeds + discovered), built
    by the caller; when None, assembled from settings alone.
    loc_prefs: normalized location preferences (see services.locations).
    only: when given, run just these sources. A full cycle takes minutes, which
    makes testing one adapter painfully slow; restricting it turns that into
    seconds. Skipped sources report as disabled rather than silently absent.
    """
    from app.services.ats_discovery import build_ats_slugs
    from app.services.locations import adzuna_countries, jobicy_geos

    if ats_slugs is None:
        ats_slugs = build_ats_slugs(cfg)
    adzuna_country_codes = adzuna_countries(loc_prefs or {})
    jobicy_geo_list = jobicy_geos(loc_prefs or {})

    all_jobs: list[dict] = []
    stats: dict = {}

    # Some adapters cache within a cycle (a Wellfound role page and the
    # Arbeitnow feed are identical for every location, so re-downloading them
    # per location is pure waste). That caching must not outlive the cycle:
    # otherwise a manual re-trigger after an adapter change returns the old
    # results and looks like the change did nothing.
    _reset_source_caches()

    resting = resting or {}
    if manual is None:
        manual = only is not None

    def _disable(source: str, reason: str = "") -> None:
        """
        Record a source as not run this cycle.

        Never overwrites a reason already recorded. Every source's branch ends
        in an `else` that marks it disabled, and those used to clobber the more
        specific explanation `_skip` had just written — so a rested source
        reported as plainly "disabled" and the reason it was rested went
        nowhere.
        """
        existing = stats.get(source)
        if existing is not None and not existing.get("enabled", True):
            return
        stats[source] = {
            "count": 0, "enabled": False, "errors": [reason] if reason else [],
        }

    def _skip(source: str) -> bool:
        """True when this source is not being called; records why."""
        if only is not None and source not in only:
            _disable(source)
            return True
        # A source that has failed every run for weeks answers identically
        # again today. Skipped rather than removed: a probe goes out
        # periodically, so a refreshed key is picked up on its own.
        #
        # Not on a manual run. A source asked for by name on the runs page is
        # exactly how somebody checks whether the key they just fixed works —
        # resting is an automatic economy, not a lock.
        #
        # "Manual" is its own flag now. It used to be read as `only is None`,
        # but every scheduled group run passes its group's sources as `only`,
        # so resting never applied to any scheduled run at all.
        if _rests(source):
            _disable(source, _resting_reason(source))
            return True
        return False

    def _rests(source: str) -> bool:
        return not manual and source in resting

    def _resting_reason(source: str) -> str:
        return (
            f"resting after failing {resting[source]} runs in a row; "
            f"re-probed periodically, or fix its credentials to resume "
            f"immediately"
        )

    # --- Tier 1: httpx adapters ---

    if cfg.ADZUNA_APP_ID and cfg.ADZUNA_APP_KEY and not _skip("adzuna"):
        from app.services.sources.adzuna import fetch as adzuna_fetch
        stats.setdefault("adzuna", {"count": 0, "errors": [], "enabled": True})
        # Adzuna has one API endpoint per country: search country-wide in each
        # preferred country instead of passing location text to the US endpoint.
        for role in roles:
            for country in (adzuna_country_codes or ["us"]):
                try:
                    jobs = adzuna_fetch(app_id=cfg.ADZUNA_APP_ID, app_key=cfg.ADZUNA_APP_KEY,
                                       query=role, location="", country=country)
                    _record(stats, "adzuna", jobs)
                    all_jobs.extend(jobs)
                except Exception as exc:
                    _record(stats, "adzuna", [], f"{role}/{country}: {exc}")
    else:
        _disable("adzuna")

    if cfg.JSEARCH_API_KEY and not _skip("jsearch"):
        from app.services.sources.jsearch import fetch as jsearch_fetch
        _run_combos(
            stats, all_jobs, "jsearch",
            lambda role, loc: jsearch_fetch(
                api_key=cfg.JSEARCH_API_KEY, query=role, location=loc,
                num_pages=getattr(cfg, "JSEARCH_NUM_PAGES", 1),
                date_posted=getattr(cfg, "JSEARCH_DATE_POSTED", "3days")),
            [(r, l) for r in roles for l in locations],
            _skip,
        )
    else:
        _disable("jsearch")

    greenhouse_slugs = ats_slugs.get("greenhouse") or []
    if greenhouse_slugs and not _skip("greenhouse"):
        from app.services.sources.greenhouse import fetch as gh_fetch
        try:
            jobs = gh_fetch(company_slugs=greenhouse_slugs,
                            max_age_days=getattr(cfg, "MAX_JOB_AGE_DAYS", None))
            _record(stats, "greenhouse", jobs)
            all_jobs.extend(jobs)
        except Exception as exc:
            _record(stats, "greenhouse", [], str(exc))
        stats.setdefault("greenhouse", {"count": 0, "errors": [], "enabled": True})
        stats["greenhouse"]["enabled"] = True
    else:
        _disable("greenhouse")

    lever_slugs = ats_slugs.get("lever") or []
    if lever_slugs and not _skip("lever"):
        from app.services.sources.lever import fetch as lever_fetch
        try:
            jobs = lever_fetch(company_slugs=lever_slugs,
                               max_age_days=getattr(cfg, "MAX_JOB_AGE_DAYS", None))
            _record(stats, "lever", jobs)
            all_jobs.extend(jobs)
        except Exception as exc:
            _record(stats, "lever", [], str(exc))
        stats.setdefault("lever", {"count": 0, "errors": [], "enabled": True})
        stats["lever"]["enabled"] = True
    else:
        _disable("lever")

    ashby_slugs = ats_slugs.get("ashby") or []
    if ashby_slugs and not _skip("ashby"):
        from app.services.sources.ashby import fetch as ashby_fetch
        try:
            jobs = ashby_fetch(company_slugs=ashby_slugs,
                               max_age_days=getattr(cfg, "MAX_JOB_AGE_DAYS", None))
            _record(stats, "ashby", jobs)
            all_jobs.extend(jobs)
        except Exception as exc:
            _record(stats, "ashby", [], str(exc))
        stats.setdefault("ashby", {"count": 0, "errors": [], "enabled": True})
        stats["ashby"]["enabled"] = True
    else:
        _disable("ashby")

    # --- Additional slug-based ATS boards (no keys; slugs configured or auto-discovered) ---
    for ats_name, fetch_path in (
        ("smartrecruiters", "app.services.sources.smartrecruiters"),
        ("workable", "app.services.sources.workable"),
        ("recruitee", "app.services.sources.recruitee"),
        ("icims", "app.services.sources.icims"),
        ("bamboohr", "app.services.sources.bamboohr"),
        ("teamtailor", "app.services.sources.teamtailor"),
        ("jobvite", "app.services.sources.jobvite"),
        ("personio", "app.services.sources.personio"),
    ):
        slugs = ats_slugs.get(ats_name) or []
        if slugs and not _skip(ats_name):
            import importlib
            ats_fetch = importlib.import_module(fetch_path).fetch
            stats.setdefault(ats_name, {"count": 0, "errors": [], "enabled": True})
            try:
                jobs = ats_fetch(company_slugs=slugs)
                _record(stats, ats_name, jobs)
                all_jobs.extend(jobs)
            except Exception as exc:
                _record(stats, ats_name, [], str(exc))
        else:
            _disable(ats_name)

    # --- Workday-hosted career sites (tenant:host:site triples) ---
    workday_tenants = ats_slugs.get("workday") or []
    if workday_tenants and not _skip("workday"):
        from app.services.sources.workday import fetch as workday_fetch
        stats.setdefault("workday", {"count": 0, "errors": [], "enabled": True})
        try:
            jobs = workday_fetch(tenant_specs=workday_tenants, queries=roles)
            _record(stats, "workday", jobs)
            all_jobs.extend(jobs)
        except Exception as exc:
            _record(stats, "workday", [], str(exc))
    else:
        _disable("workday")

    # --- Jooble: keyed aggregator (free key) ---
    if cfg.JOOBLE_API_KEY and not _skip("jooble"):
        from app.services.sources.jooble import fetch as jooble_fetch
        stats.setdefault("jooble", {"count": 0, "errors": [], "enabled": True})
        for role in roles:
            for loc in locations:
                try:
                    jobs = jooble_fetch(api_key=cfg.JOOBLE_API_KEY, query=role, location=loc)
                    _record(stats, "jooble", jobs)
                    all_jobs.extend(jobs)
                except Exception as exc:
                    _record(stats, "jooble", [], f"{role}/{loc}: {exc}")
    else:
        _disable("jooble")

    # --- Careerjet: keyed aggregator (free affiliate id) ---
    if cfg.CAREERJET_AFFID and not _skip("careerjet"):
        from app.services.sources.careerjet import fetch as careerjet_fetch
        stats.setdefault("careerjet", {"count": 0, "errors": [], "enabled": True})
        for role in roles:
            for loc in locations:
                try:
                    jobs = careerjet_fetch(affid=cfg.CAREERJET_AFFID, query=role, location=loc)
                    _record(stats, "careerjet", jobs)
                    all_jobs.extend(jobs)
                except Exception as exc:
                    _record(stats, "careerjet", [], f"{role}/{loc}: {exc}")
    else:
        _disable("careerjet")

    # --- USAJOBS: the federal government's official API (free key) ---
    # The only source that states pay on every posting, because federal salary
    # ranges are public by law — so those land in the salary columns directly.
    if cfg.USAJOBS_API_KEY and cfg.USAJOBS_USER_AGENT and not _skip("usajobs"):
        from app.services.sources.usajobs import fetch as usajobs_fetch
        stats.setdefault("usajobs", {"count": 0, "errors": [], "enabled": True})
        _run_combos(
            stats, all_jobs, "usajobs",
            lambda role, loc: usajobs_fetch(
                api_key=cfg.USAJOBS_API_KEY, user_agent=cfg.USAJOBS_USER_AGENT,
                query=role, location=loc,
                max_pages=getattr(cfg, "USAJOBS_MAX_PAGES", 2),
            ),
            [(r, l) for r in roles for l in locations],
            _skip,
        )
    else:
        # Half-configured is the failure worth naming: the API 401s a request
        # carrying only one of the two, which reads as a bad key.
        _disable(
            "usajobs",
            "" if not (cfg.USAJOBS_API_KEY or cfg.USAJOBS_USER_AGENT) else
            "USAJOBS needs BOTH USAJOBS_API_KEY and USAJOBS_USER_AGENT (the "
            "email you registered with); it answers 401 to a request carrying "
            "only one",
        )

    # --- hiring.cafe: aggregates ATS boards, so descriptions come with it ---
    if getattr(cfg, "HIRINGCAFE_ENABLED", True) and not _skip("hiringcafe"):
        from app.services.sources.hiringcafe import fetch as hiringcafe_fetch
        _run_combos(
            stats, all_jobs, "hiringcafe",
            lambda role, loc: hiringcafe_fetch(query=role, location=loc),
            [(r, l) for r in roles for l in locations],
            _skip,
        )
    else:
        _disable("hiringcafe")

    # --- Y Combinator: fixed role pages, fetched once for the whole cycle ---
    if getattr(cfg, "YC_ENABLED", True) and not _skip("ycombinator"):
        from app.services.sources.ycombinator import fetch as yc_fetch
        stats.setdefault("ycombinator", {"count": 0, "errors": [], "enabled": True})
        try:
            jobs = yc_fetch()
            _record(stats, "ycombinator", jobs)
            all_jobs.extend(jobs)
        except Exception as exc:
            _record(stats, "ycombinator", [], str(exc))
    else:
        _disable("ycombinator")

    # --- Findwork: keyed developer-jobs API (free key) ---
    if cfg.FINDWORK_API_KEY and not _skip("findwork"):
        from app.services.sources.findwork import fetch as findwork_fetch
        stats.setdefault("findwork", {"count": 0, "errors": [], "enabled": True})
        for role in roles:
            try:
                jobs = findwork_fetch(api_key=cfg.FINDWORK_API_KEY, query=role)
                _record(stats, "findwork", jobs)
                all_jobs.extend(jobs)
            except Exception as exc:
                _record(stats, "findwork", [], f"{role}: {exc}")
    else:
        _disable("findwork")

    # --- LinkedIn: httpx guest API (no browser needed) ---
    # One call for the whole cycle: the same posting appears under many
    # query/location pairs, and deduping before the description fetches keeps
    # the detail budget going to distinct jobs.
    if not _skip("linkedin"):
        from app.services.sources.linkedin import fetch_all as li_fetch_all
        stats.setdefault("linkedin", {"count": 0, "errors": [], "enabled": True})
        try:
            jobs = li_fetch_all(
                session_cookie=cfg.LINKEDIN_SESSION_COOKIE, queries=roles,
                locations=locations,
                # Read off cfg rather than left to the adapter's own settings
                # lookup, so the UI overrides in the overlay actually reach it.
                max_pages=getattr(cfg, "LINKEDIN_MAX_PAGES", None),
                recency_hours=getattr(cfg, "LINKEDIN_RECENCY_HOURS", None),
            )
            _record(stats, "linkedin", jobs)
            all_jobs.extend(jobs)
        except Exception as exc:
            _record(stats, "linkedin", [], str(exc))

    # --- Indeed: RSS feed, retired upstream (every query 404s) ---
    if getattr(cfg, "INDEED_RSS_ENABLED", False) and not _skip("indeed"):
        from app.services.sources.indeed import fetch as indeed_fetch
        _run_combos(
            stats, all_jobs, "indeed",
            lambda role, loc: indeed_fetch(query=role, location=loc),
            [(r, l) for r in roles for l in locations],
            _skip,
        )
    else:
        _disable("indeed", "Indeed retired its public RSS feed (404 for every "
                       "query); set INDEED_RSS_ENABLED=true to retry it")

    # --- Remotive: free public API for remote tech jobs ---
    from app.services.sources.remotive import fetch as remotive_fetch
    _run_combos(stats, all_jobs, "remotive",
                lambda role: remotive_fetch(query=role), [(r,) for r in roles], _skip)

    # --- Arbeitnow: free public feed, downloaded once and filtered per query ---
    from app.services.sources.arbeitnow import fetch as arbeitnow_fetch
    _run_combos(
        stats, all_jobs, "arbeitnow",
        lambda role, loc: arbeitnow_fetch(
            query=role, location=loc,
            max_pages=getattr(cfg, "ARBEITNOW_MAX_PAGES", 3)),
        [(r, l) for r in roles for l in locations],
        _skip,
        )

    # --- RemoteOK: free public API for remote tech jobs ---
    from app.services.sources.remoteok import fetch as remoteok_fetch
    _run_combos(stats, all_jobs, "remoteok",
                lambda role: remoteok_fetch(query=role), [(r,) for r in roles], _skip)

    # --- We Work Remotely: RSS feed for remote tech jobs ---
    from app.services.sources.weworkremotely import fetch as wwr_fetch
    _run_combos(stats, all_jobs, "weworkremotely",
                lambda role: wwr_fetch(query=role), [(r,) for r in roles], _skip)

    # --- The Muse: free public API, tech categories ---
    from app.services.sources.themuse import fetch as themuse_fetch
    _run_combos(stats, all_jobs, "themuse",
                lambda role: themuse_fetch(query=role), [(r,) for r in roles], _skip)

    # --- Himalayas: free public API for remote tech jobs ---
    from app.services.sources.himalayas import fetch as himalayas_fetch
    _run_combos(stats, all_jobs, "himalayas",
                lambda role: himalayas_fetch(query=role), [(r,) for r in roles], _skip)

    # --- Jobicy: free public API for remote tech jobs (region-targeted) ---
    from app.services.sources.jobicy import fetch as jobicy_fetch
    _run_combos(stats, all_jobs, "jobicy",
                lambda role, geo: jobicy_fetch(query=role, geo=geo),
                [(r, g) for r in roles for g in jobicy_geo_list], _skip)

    # --- Hacker News "Who is hiring?": one monthly thread, fetched once ---
    if not _skip("hnhiring"):
        from app.services.sources.hnhiring import fetch as hn_fetch
        stats.setdefault("hnhiring", {"count": 0, "errors": [], "enabled": True})
        try:
            jobs = hn_fetch(queries=roles)
            _record(stats, "hnhiring", jobs)
            all_jobs.extend(jobs)
        except Exception as exc:
            _record(stats, "hnhiring", [], str(exc))

    # --- Working Nomads: free public API for remote jobs ---
    from app.services.sources.workingnomads import fetch as wn_fetch
    _run_combos(stats, all_jobs, "workingnomads",
                lambda role: wn_fetch(query=role), [(r,) for r in roles], _skip)

    # --- Built In: tech-focused job board with city hubs ---
    if getattr(cfg, "BUILTIN_ENABLED", True) and not _skip("builtin"):
        from app.services.sources.builtin import fetch as builtin_fetch
        _run_combos(stats, all_jobs, "builtin",
                    lambda role: builtin_fetch(query=role), [(r,) for r in roles], _skip)
    else:
        _disable("builtin")

    # --- Jobspresso: curated remote jobs RSS feed ---
    from app.services.sources.jobspresso import fetch as jobspresso_fetch
    _run_combos(stats, all_jobs, "jobspresso",
                lambda role: jobspresso_fetch(query=role), [(r,) for r in roles], _skip)

    # --- Tier 2: Playwright scrapers (Wellfound, Dice, Handshake) ---
    # Launching a browser is the most expensive thing here, so don't do it at
    # all when none of its sources were asked for.
    pw_sources = {"wellfound", "dice", "handshake"}
    # Resting applies here too; these branches never went through `_skip`.
    pw_rested = {src for src in pw_sources if _rests(src)}
    wanted_pw = (pw_sources if only is None else pw_sources & only) - pw_rested
    run_browser_tier = bool(wanted_pw)

    async def _run_playwright() -> tuple[list[dict], dict]:
        pw_jobs: list[dict] = []
        pw_stats: dict = {}

        if "wellfound" in wanted_pw and getattr(
            cfg, "WELLFOUND_ENABLED", True
        ):
            # Wellfound is scraped by role page, not by search query: the
            # pages are a fixed taxonomy and carry no location, so one pass over
            # the configured roles covers every query/location combination.
            from app.services.sources.wellfound import fetch_roles as wf_fetch_roles
            pw_stats.setdefault("wellfound", {"count": 0, "errors": [], "enabled": True})
            try:
                jobs = await wf_fetch_roles(location=locations[0] if locations else "")
                _record(pw_stats, "wellfound", jobs)
                pw_jobs.extend(jobs)
            except Exception as exc:
                _record(pw_stats, "wellfound", [], str(exc))
        else:
            pw_stats["wellfound"] = {"count": 0, "errors": [], "enabled": False}

        if "dice" in wanted_pw and getattr(cfg, "DICE_ENABLED", True):
            from app.services.sources.dice import fetch as dice_fetch
            pw_stats.setdefault("dice", {"count": 0, "errors": [], "enabled": True})
            for role in roles:
                for loc in locations:
                    try:
                        jobs = await dice_fetch(query=role, location=loc)
                        _record(pw_stats, "dice", jobs)
                        pw_jobs.extend(jobs)
                    except Exception as exc:
                        _record(pw_stats, "dice", [], f"{role}/{loc}: {exc}")
        else:
            pw_stats["dice"] = {"count": 0, "errors": [], "enabled": False}

        if getattr(cfg, "HANDSHAKE_SESSION_COOKIE", "") and "handshake" in wanted_pw:
            from app.services.sources.handshake import fetch as hs_fetch
            pw_stats.setdefault("handshake", {"count": 0, "errors": [], "enabled": True})
            for role in roles:
                try:
                    jobs = await hs_fetch(session_cookie=cfg.HANDSHAKE_SESSION_COOKIE,
                                          query=role, location="")
                    _record(pw_stats, "handshake", jobs)
                    pw_jobs.extend(jobs)
                except Exception as exc:
                    _record(pw_stats, "handshake", [], f"{role}: {exc}")
        else:
            pw_stats["handshake"] = {"count": 0, "errors": [], "enabled": False}

        return pw_jobs, pw_stats

    try:
        if not run_browser_tier:
            raise _BrowserTierSkipped
        pw_jobs, pw_stats = asyncio.run(_run_playwright())
        all_jobs.extend(pw_jobs)
        stats.update(pw_stats)
    except _BrowserTierSkipped:
        for src in sorted(pw_sources):
            _disable(src)
    except Exception as exc:
        logger.error("Playwright scrapers fatal error: %s", exc)
        for src in ("wellfound", "dice", "handshake"):
            stats.setdefault(src, {"count": 0, "errors": [str(exc)], "enabled": True})
    for src in pw_rested:
        stats[src] = {"count": 0, "enabled": False, "errors": [_resting_reason(src)]}

    # Log summary
    logger.info("=== fetch summary ===")
    for source, s in stats.items():
        status = "disabled" if not s["enabled"] else (
            f"OK {s['count']} jobs" if not s["errors"] else
            f"PARTIAL {s['count']} jobs, {len(s['errors'])} error(s)"
            if s["count"] > 0 else
            f"FAILED {len(s['errors'])} error(s)"
        )
        logger.info("  %-12s %s", source, status)
        for err in s["errors"]:
            logger.warning("    └─ %s", err)

    return all_jobs, stats


def _resting_sources(db: Session) -> dict:
    """
    Sources to skip this cycle for having failed every run for weeks.

    Never fails the cycle: not knowing which sources are resting costs a few
    wasted requests, and a history query that errors must not cost the fetch.
    """
    try:
        from app.services.fetch_history import resting_sources

        resting = resting_sources(
            db,
            threshold=settings.SOURCE_REST_AFTER_FAILURES,
            retry_every=settings.SOURCE_REST_RETRY_EVERY,
        )
        if resting:
            logger.info(
                "job_fetcher: resting %s (failing every run for a long time)",
                ", ".join(f"{s} ×{n}" for s, n in sorted(resting.items())),
            )
        return resting
    except Exception as exc:
        logger.warning("job_fetcher: could not read failing-source history: %s", exc)
        return {}


def _known_urls(db: Session) -> set[str]:
    """Every URL already attached to a stored job, listing or apply."""
    known: set[str] = set()
    for url, source_urls, apply_url in db.query(Job.url, Job.source_urls, Job.apply_url):
        if url:
            known.add(url)
        if apply_url:
            known.add(apply_url)
        known.update(u for u in (source_urls or []) if u)
    return known


def _resolve_apply_links(db: Session, raw_jobs: list[dict]):
    """
    Turn aggregator interstitials into real apply URLs, in place.

    Restricted to postings we've never stored, so steady-state cycles spend
    almost no requests here — a job's apply link is resolved exactly once.
    """
    from app.services.link_resolver import is_interstitial, resolve_jobs

    known = _known_urls(db)
    fresh = [
        job for job in raw_jobs
        if (job.get("url") or "") not in known and is_interstitial(job.get("url") or "")
    ]
    if not fresh:
        return None
    return resolve_jobs(
        fresh,
        max_links=settings.LINK_RESOLVE_MAX_PER_CYCLE,
        workers=settings.LINK_RESOLVE_WORKERS,
        per_host=settings.LINK_RESOLVE_PER_HOST,
        host_delay=settings.LINK_RESOLVE_HOST_DELAY_MS / 1000.0,
    )


def _name_board_jobs(db: Session, raw_jobs: list[dict]) -> int:
    """
    Put the employer's name on jobs that arrived filed under a board slug.

    Greenhouse, Workday and the listing readers only know the slug, so a
    posting came in as company "doordashusa" — and the same opening from
    LinkedIn as "DoorDash" hashed differently and was stored twice, while an
    excluded-companies entry for "DoorDash" never matched it. The registry
    already holds the board's own name (validation refiles it from the board's
    API); this uses it. Only a company that *is* the slug, or is blank, is
    replaced — a name the adapter actually read is left alone.
    """
    from app.models.company_board import CompanyBoard

    wanted: dict[tuple[str, str], list[dict]] = {}
    for job in raw_jobs:
        slug = job.get("ats_slug")
        if not slug:
            continue
        company = (job.get("company") or "").strip()
        if company and company.lower() != str(slug).lower():
            continue
        wanted.setdefault((job.get("source", ""), str(slug)), []).append(job)
    if not wanted:
        return 0

    names: dict[tuple[str, str], str] = {}
    sources = {ats for ats, _ in wanted}
    slugs = {slug for _, slug in wanted}
    rows = (
        db.query(CompanyBoard.ats, CompanyBoard.slug, CompanyBoard.company)
        .filter(CompanyBoard.ats.in_(sources), CompanyBoard.slug.in_(slugs),
                CompanyBoard.company.isnot(None))
        .all()
    )
    for ats, slug, company in rows:
        if company and company.strip() and company.strip().lower() != slug.lower():
            names[(ats, slug)] = company.strip()

    renamed = 0
    for key, jobs in wanted.items():
        name = names.get(key)
        if not name:
            continue
        for job in jobs:
            job["company"] = name
            renamed += 1
    return renamed


def _maybe_backfill_boards(db: Session, profile) -> dict | None:
    """
    Mine the pre-registry jobs table, once, on the first cycle after deploy.

    Discovery, link resolution and sniffing only ever see freshly fetched
    postings, so without this the whole back catalogue — the richest source of
    company boards we have — stays unread. Running it here rather than as a
    manual step means the boards it recovers are available to this very cycle's
    fetch, and nobody has to remember to trigger anything.

    Recorded on the profile so it happens exactly once. Failures are retried on
    later cycles but give up after a few attempts rather than re-running an
    expensive scan forever.
    """
    import copy

    if not settings.BOARD_BACKFILL_ON_START:
        return None

    state = (profile.data or {}).get("board_backfill") or {}
    if state.get("done"):
        return None
    attempts = state.get("attempts", 0)
    if attempts >= _MAX_BACKFILL_ATTEMPTS:
        return None

    from app.services.board_backfill import backfill_boards

    logger.info("job_fetcher: running one-time board backfill (attempt %d)", attempts + 1)
    try:
        with db.begin_nested():
            report = backfill_boards(
                db,
                max_links=settings.BOARD_BACKFILL_MAX_LINKS,
                max_hosts=settings.BOARD_BACKFILL_MAX_HOSTS,
                workers=settings.BOARD_BACKFILL_WORKERS,
                commit=False,
            )
        record = {"done": True, "at": datetime.now(timezone.utc).isoformat(),
                  **report.as_dict()}
    except Exception as exc:
        logger.error("job_fetcher: board backfill failed: %s", exc)
        record = {"done": False, "attempts": attempts + 1, "error": str(exc)[:200]}
        report = None

    data = copy.deepcopy(profile.data)
    data["board_backfill"] = record
    profile.data = data
    db.commit()
    return report.as_dict() if report else None


def _update_board_registry(
    db: Session,
    raw_jobs: list[dict],
    ats_slugs: dict,
    source_stats: dict,
    resolve_stats,
    updated_data: dict,
) -> dict:
    """
    Fold this cycle's findings back into the board registry:
    new boards spotted in job links and resolved apply URLs, boards sniffed off
    company careers sites, and how many jobs each polled board returned.
    """
    from app.services import company_boards as boards
    from app.services.ats_discovery import discover_from_jobs

    stats: dict = {}

    found = discover_from_jobs(raw_jobs)
    stats["discovered"] = boards.record_boards(db, found, origin="discovered")

    # Career sites that aren't a recognised ATS: sniff them for an embedded
    # board. The landing HTML from link resolution often answers for free.
    if settings.ATS_SNIFF_CAREER_SITES:
        stats["sniffed"] = _sniff_career_sites(db, raw_jobs, resolve_stats, updated_data)

    # Per-board yield, so next cycle's budget favours boards that produce.
    for ats, attempted in (ats_slugs or {}).items():
        if not attempted:
            continue
        # Only for ATSes this run actually polled. A run that did not touch
        # Greenhouse has no evidence about any Greenhouse board, and counting
        # its silence would tick every one of them toward retirement — eight
        # API-only cycles would have retired the entire board registry.
        if not (source_stats.get(ats) or {}).get("enabled", False):
            continue
        per_slug: dict[str, int] = {}
        for job in raw_jobs:
            if job.get("source") == ats and job.get("ats_slug"):
                per_slug[job["ats_slug"]] = per_slug.get(job["ats_slug"], 0) + 1
        boards.record_fetch_results(
            db, ats, attempted, per_slug,
            had_errors=bool((source_stats.get(ats) or {}).get("errors")),
            max_empty_cycles=settings.ATS_BOARD_MAX_EMPTY_CYCLES,
        )

    return stats


def _sniff_career_sites(db: Session, raw_jobs: list[dict], resolve_stats,
                        updated_data: dict) -> int:
    """Mine company careers sites for the ATS board behind them."""
    from app.services import company_boards as boards
    from app.services.ats_discovery import ALL_ATS
    from app.services.ats_sniffer import company_host, sniff_hosts
    from app.services.link_resolver import is_aggregator

    landing_html = resolve_stats.landing_html if resolve_stats else {}

    # Candidates are apply URLs we resolved out of aggregator redirects *and*
    # the many sources (Remotive, RemoteOK, HN, The Muse, ...) that link
    # straight at the employer's own site to begin with.
    hosts: dict[str, str] = {}   # host → landing HTML, "" meaning "go fetch it"
    host_company: dict[str, str] = {}
    for job in raw_jobs:
        if job.get("source") in ALL_ATS:
            continue  # already a board we poll directly
        candidate = job.get("apply_url") or job.get("url") or ""
        if not candidate or is_aggregator(candidate):
            continue
        host = company_host(candidate)
        if not host:
            continue
        html = landing_html.get(job.get("url") or "", "")
        if html or host not in hosts:
            hosts[host] = html or hosts.get(host, "")
        if job.get("company"):
            host_company.setdefault(host, job["company"])

    if not hosts:
        return 0

    merged, cache, per_host = sniff_hosts(
        hosts,
        updated_data.get("ats_sniff_cache"),
        max_hosts=settings.ATS_SNIFF_MAX_HOSTS_PER_CYCLE,
    )
    updated_data["ats_sniff_cache"] = cache

    new_boards = 0
    for host, found in per_host.items():
        new_boards += boards.record_boards(
            db, found, origin="sniffed",
            company=host_company.get(host), source_host=host,
        )
    return new_boards


def fetch_and_save_jobs(
    db: Session, only: set[str] | None = None, group: str | None = None,
) -> dict:
    """
    Run one fetch cycle.

    `only` restricts it to the named sources, which is what makes testing a
    single adapter take seconds instead of minutes. `group` does the same from
    the other end — "api", "boards" or "browser" — and is how the scheduled
    cycles run: each slice on the cadence it deserves rather than all of them
    behind the slowest one. Passing both is fine; `only` wins, because it is
    the more specific request.
    """
    started_at = datetime.now(timezone.utc)
    # Named sources are a person on the runs page; a group is the schedule.
    manual = only is not None
    if only is None:
        only = group_sources(group)
    counts = {"fetched": 0, "inserted": 0, "merged": 0, "skipped": 0, "stale": 0,
              "dropped": 0, "sources": {}, "group": group or "all"}

    profile = db.query(Profile).first()
    if not profile:
        logger.warning("job_fetcher: no profile found, skipping.")
        return counts

    roles: list[str] = profile.data.get("target_roles") or []

    # Structured location preferences drive the search locations, Adzuna
    # country endpoints, and the region prefilter during matching.
    from app.services.locations import normalize_prefs, search_locations
    loc_prefs = normalize_prefs(profile.data)
    locations: list[str] = search_locations(loc_prefs)

    if not roles:
        logger.warning("job_fetcher: target_roles empty.")
        return counts

    # Expand target roles into the fuller set of queries recruiters post under
    # (cached on the profile; falls back to the raw roles if the LLM is down).
    from app.services.query_expansion import expand_search_queries
    query_cache = None
    try:
        queries, query_cache = expand_search_queries(
            profile.data, settings.NVIDIA_NIM_API_KEY,
            settings.NVIDIA_NIM_BASE_URL, settings.NVIDIA_NIM_MODEL,
        )
    except Exception as exc:
        logger.error("job_fetcher: query expansion failed: %s", exc)
        queries = list(roles)
    if not queries:
        queries = list(roles)

    discovered_ats = (
        profile.data.get("discovered_ats") if settings.ATS_AUTO_DISCOVERY else None
    )

    # Harvest company ATS slugs from community job lists (e.g. the SimplifyJobs
    # new-grad README) and fold them into the discovered set.
    if settings.ATS_LIST_HARVEST and settings.SLUG_HARVEST_URLS:
        try:
            from app.services.ats_discovery import harvest_slugs_from_lists
            harvest_urls = [u.strip() for u in settings.SLUG_HARVEST_URLS.split(",") if u.strip()]
            discovered_ats = harvest_slugs_from_lists(harvest_urls, discovered_ats)
        except Exception as exc:
            logger.error("job_fetcher: slug harvest failed: %s", exc)

    # Validate/auto-fix the configured ATS slugs (cached per slug on the profile),
    # then assemble the final slug map: configured + verified seeds + discovered.
    from app.services.ats_discovery import build_ats_slugs, configured_ats_slugs, slug_caps
    slug_cache = None
    slug_report: dict = {}
    validated_configured = None
    if settings.ATS_SLUG_VALIDATION:
        try:
            from app.services.ats_validation import validate_configured_slugs
            validated_configured, slug_cache, slug_report = validate_configured_slugs(
                configured_ats_slugs(settings), profile.data.get("ats_slug_cache")
            )
        except Exception as exc:
            logger.error("job_fetcher: slug validation failed: %s", exc)

    # The board registry is the durable store of every company ATS board we've
    # learned about, ranked by what each one actually yields. Legacy slugs from
    # the old profile blob are folded in on the way past.
    registry_boards = None
    backfill_report = None
    if settings.ATS_BOARD_REGISTRY:
        try:
            from app.services import company_boards as boards
            # Savepoint, not the whole transaction: a registry problem must not
            # discard the query cache or the jobs this cycle is about to save.
            with db.begin_nested():
                if discovered_ats:
                    boards.backfill_from_slugs(db, discovered_ats, origin="discovered")
                if settings.ATS_SEED_COMPANIES:
                    from app.services.ats_seeds import SEED_ATS_SLUGS
                    boards.backfill_from_slugs(db, SEED_ATS_SLUGS, origin="seed")
                if validated_configured:
                    boards.backfill_from_slugs(db, validated_configured, origin="configured")
            db.commit()
            # Before picking this cycle's slugs, so anything the backfill
            # recovers from the back catalogue is fetched straight away.
            backfill_report = _maybe_backfill_boards(db, profile)
            # And before that selection too: a board nobody has confirmed
            # exists is not polled, so the per-ATS budget goes to companies
            # rather than to slugs scraped off an aggregator's own page.
            if settings.ATS_BOARD_VALIDATION:
                try:
                    with db.begin_nested():
                        boards.validate_pending(
                            db,
                            limit=settings.ATS_BOARD_VALIDATE_PER_CYCLE,
                            workers=settings.ATS_BOARD_FETCH_WORKERS,
                        )
                    db.commit()
                except Exception as exc:
                    logger.error("job_fetcher: board validation failed: %s", exc)
                    db.rollback()
            registry_boards = boards.registry_slugs(db, slug_caps())
        except Exception as exc:
            logger.error("job_fetcher: board registry unavailable: %s", exc)
            registry_boards = None

    ats_slugs = build_ats_slugs(
        settings, discovered_ats, validated_configured, registry_boards
    )

    # Adapters handle their own failures and return [], so the reason a source
    # produced nothing lives only in its log line. Capture those and attach them
    # to the stats, otherwise a blocked source is indistinguishable from a
    # search that genuinely had no matches.
    from app.services.source_diagnostics import SourceLogCapture, merge_into_stats
    try:
        # The overlay is `settings` with the profile's UI overrides on top, so
        # every adapter picks them up through the `cfg.X` reads it already does.
        from app.services.tunables import effective_settings
        cfg = effective_settings(profile.data)
        with SourceLogCapture() as capture:
            raw_jobs, source_stats = _run_all_adapters(
                queries, locations, cfg, ats_slugs, loc_prefs, only,
                resting=_resting_sources(db), manual=manual,
            )
        merge_into_stats(source_stats, capture.messages, capture.errors)
    except Exception as exc:
        logger.error("job_fetcher: _run_all_adapters failed: %s", exc)
        return counts

    counts["fetched"] = len(raw_jobs)
    counts["sources"] = source_stats
    now = datetime.now(timezone.utc)

    # Follow aggregator redirect pages through to the employer's own apply link.
    # Only postings we haven't seen before are worth the round trip.
    resolve_stats = None
    if settings.RESOLVE_APPLY_LINKS:
        try:
            resolve_stats = _resolve_apply_links(db, raw_jobs)
        except Exception as exc:
            logger.error("job_fetcher: apply-link resolution failed: %s", exc)

    if settings.ATS_BOARD_REGISTRY:
        try:
            counts["board_names"] = _name_board_jobs(db, raw_jobs)
        except Exception as exc:
            logger.error("job_fetcher: naming board jobs failed: %s", exc)
            db.rollback()

    # Persist last fetch stats on the profile so UI can show them
    import copy
    updated_data = copy.deepcopy(profile.data)
    if query_cache:
        updated_data["search_query_cache"] = query_cache
    if slug_cache is not None:
        updated_data["ats_slug_cache"] = slug_cache
    if slug_report:
        updated_data["ats_slug_report"] = slug_report

    # Learn company ATS boards from the fetched jobs' links; the merged slug
    # list feeds the direct board fetches on the next cycle.
    if settings.ATS_AUTO_DISCOVERY:
        try:
            from app.services.ats_discovery import discover_ats_slugs
            updated_data["discovered_ats"] = discover_ats_slugs(raw_jobs, discovered_ats)
        except Exception as exc:
            logger.error("job_fetcher: ATS discovery failed: %s", exc)

    board_stats: dict = {}
    if settings.ATS_BOARD_REGISTRY:
        try:
            with db.begin_nested():
                board_stats = _update_board_registry(
                    db, raw_jobs, ats_slugs, source_stats,
                    resolve_stats, updated_data,
                )
            db.commit()
            from app.services.company_boards import summary
            board_stats["registry"] = summary(db)
        except Exception as exc:
            logger.error("job_fetcher: board registry update failed: %s", exc)
            board_stats = {}

    # Hand what the server could not follow to the browser. Never blocks and
    # never fails the cycle: if no agent is listening the tasks simply expire.
    if settings.RESOLVE_APPLY_LINKS:
        try:
            from app.services.agent_work import enqueue_unresolved_links
            counts["links_queued_to_browser"] = enqueue_unresolved_links(db)
        except Exception as exc:
            logger.error("job_fetcher: queueing browser link resolution failed: %s", exc)

    updated_data["last_fetch"] = {
        "at": now.isoformat(),
        "fetched": len(raw_jobs),
        "sources": {
            src: {"count": s["count"], "enabled": s["enabled"],
                  "errors": s["errors"][:3]}  # cap at 3 reasons stored
            for src, s in source_stats.items()
        },
        "links": resolve_stats.as_dict() if resolve_stats else None,
        "boards": board_stats or None,
        "backfill": backfill_report,
    }
    # Merge into a fresh read rather than writing `updated_data` wholesale. The
    # copy above was taken minutes ago, and other writers touch this blob in
    # the meantime — the agent poll stamps "agent" every 25 seconds, the
    # mailbox poller records its state, and a settings save can land mid-cycle.
    # Overwriting the whole blob silently reverted all of them; only the keys
    # this cycle actually owns are carried over.
    db.refresh(profile)
    merged_data = copy.deepcopy(profile.data or {})
    for key in _FETCH_CYCLE_KEYS:
        if key in updated_data:
            merged_data[key] = updated_data[key]
    # Held back rather than assigned here, and this is not tidiness.
    #
    # Assigning it now marks the row dirty, and the job loop below opens a
    # savepoint per job — `begin_nested()` flushes first, so the UPDATE lands
    # immediately and takes an exclusive lock on the profile row. That lock is
    # then held until the commit *after* every job is inserted, which on a full
    # cycle is minutes.
    #
    # Everything else that touches this blob queues behind it, and the agent
    # poll writes it every minute. Twenty-two lease requests were found stacked
    # on that lock, none completing, the browser agent dead the whole time. The
    # write happens just before the commit now, so the lock lasts milliseconds.
    counts["links"] = resolve_stats.as_dict() if resolve_stats else {}
    counts["boards"] = board_stats

    def _parse_posted_at(raw) -> datetime | None:
        if raw is None:
            return None
        if isinstance(raw, (int, float)):
            try:
                return datetime.fromtimestamp(raw, tz=timezone.utc)
            except Exception:
                return None
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except Exception:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    from app.services.tunables import value as tunable
    max_age_days = tunable(profile.data, "max_job_age_days")

    # Per-source outcomes. "Fetched" alone flatters a source that returns the
    # same postings every cycle; what matters is how many were actually new.
    per_source: dict[str, dict] = {}

    def _tally(source: str, outcome: str) -> None:
        entry = per_source.setdefault(
            source, {"inserted": 0, "merged": 0, "skipped": 0, "stale": 0, "dropped": 0}
        )
        entry[outcome] += 1

    for job_data in raw_jobs:
        # Each job gets its own savepoint: a flush that fails (a constraint
        # violation, an over-long value) used to leave the session in a failed
        # state, so every job after it errored and the final commit lost the
        # whole cycle's inserts. Rolling back to the savepoint discards only
        # the bad row.
        try:
            with db.begin_nested():
                url = job_data.get("url", "")
                source = job_data.get("source", "")
                source_job_id = job_data.get("source_job_id")
                company = job_data.get("company", "")
                title = job_data.get("title", "")
                location = job_data.get("location", "")
                # Canonicalized here rather than in each adapter, so a new
                # source cannot reintroduce HTML soup by forgetting to.
                description = clean_description(job_data.get("description", ""))
                apply_url = job_data.get("apply_url")

                # Skip stale postings: they're usually filled or unresponsive, and
                # they waste LLM matching calls and applications.
                posted_at = _parse_posted_at(job_data.get("posted_at"))
                if posted_at and max_age_days and (now - posted_at).days > max_age_days:
                    counts["stale"] += 1
                    _tally(source, "stale")
                    continue

                dedupe_hash = compute_dedupe_hash(company, title, location, url)
                existing = find_existing_job(db, source, url, source_job_id, dedupe_hash)

                if existing is not None:
                    # What this sighting knows, in the shape the shared merge
                    # reads. `posted_at` goes in already parsed: the merge
                    # refuses to guess at a date string, because a mis-parsed
                    # one silently ages a job out of the pipeline.
                    sighting = {**job_data, **_adapter_details(job_data),
                                "apply_url": apply_url, "posted_at": posted_at}

                    # Worth taking even on a job we are otherwise skipping. The
                    # pay this listing states and the last one didn't is the
                    # same windfall whether the two are cross-posts or the same
                    # posting fetched twice.
                    #
                    # This used to be an inline backfill that checked only for
                    # null — and so was the one automatic writer in the codebase
                    # that did not consult `manual_fields`. A user who cleared a
                    # wrong salary by hand had it refilled on the next cycle.
                    improved = enrich_from(existing, sighting)

                    same_row = url in existing.source_urls or (
                        source_job_id
                        and existing.source_job_id == source_job_id
                        and existing.source == source
                    )
                    if same_row:
                        # The same posting again, not a cross-post: its URL is
                        # already ours, so only the contents can be news.
                        if merge_description(existing, description):
                            improved.append("description")
                    else:
                        improved += merge_or_skip(db, existing, url, description,
                                                  layer=3, data=sighting)

                    # "Merged" means the row got better, not that it was
                    # touched. It is the number the panel reports as "enriched",
                    # and counting every cross-post as one made a source look
                    # like it was contributing when it was repeating itself.
                    outcome = "merged" if improved else "skipped"
                    counts[outcome] += 1
                    _tally(source, outcome)
                    continue

                # Seen, judged and retired months ago. Without this check
                # archiving would be worse than useless: every archived posting
                # still on its board comes back as new on the next fetch, costs
                # a scoring call, reaches the same verdict, and is archived
                # again sixty days later. There is nothing to merge into — the
                # description is what archiving discarded — so it is a skip.
                if was_archived(db, source, url, source_job_id, dedupe_hash):
                    counts["skipped"] += 1
                    _tally(source, "skipped")
                    continue

                new_job = Job(
                    source=source,
                    source_job_id=source_job_id,
                    source_urls=[url],
                    title=title,
                    company=company,
                    location=location,
                    is_remote=job_data.get("is_remote", False),
                    url=url,
                    apply_url=apply_url,
                    # NULL, not "", when cleaning found nothing worth keeping:
                    # "no description" is a state the pipeline acts on (the
                    # filter names it, enrichment goes looking for one), and it
                    # should read the same whether the source sent an empty
                    # field or a Cloudflare page.
                    description=description or None,
                    # No default. `"mid"` was never a finding — it was the
                    # fallback — and writing it made "the posting says
                    # mid-level" and "no adapter told us" the same value, which
                    # is the bug `base.parse_experience_level` and
                    # `harvest._normalize` both document at length as fixed.
                    # This ingest path was the one they missed.
                    experience_level=job_data.get("experience_level"),
                    status=JobStatus.new,
                    fetched_at=now,
                    posted_at=posted_at,
                    dedupe_hash=dedupe_hash,
                    **_adapter_details(job_data),
                )
                db.add(new_job)
                db.flush()
                counts["inserted"] += 1
                _tally(source, "inserted")

            # Committed in chunks rather than once at the end.
            #
            # The per-row savepoints isolate a bad row, which is what they were
            # for — but every one of them lived inside a single outer
            # transaction committed once, minutes later. If that commit failed
            # (a dropped connection, disk pressure) the `except` logged, rolled
            # back, and every insert in the cycle was gone. The savepoints
            # protect against a bad row, not against a bad commit.
            #
            # Outside the savepoint block, so a commit failure cannot poison a
            # flush that has already succeeded.
            if counts["inserted"] and counts["inserted"] % _COMMIT_EVERY == 0:
                try:
                    db.commit()
                except Exception as exc:
                    logger.error("job_fetcher: chunk commit failed: %s", exc)
                    db.rollback()

        except Exception as exc:
            # A job that fell out here is a job we fetched and then lost, and
            # the four outcome counters above all sum to less than `fetched`
            # without it — so the cycle reported "230 fetched, 229 accounted
            # for" and nobody could say which one went missing or why. Name the
            # posting, and count it, so a source that has started emitting rows
            # we cannot store shows up as a number instead of a discrepancy.
            counts["dropped"] += 1
            _tally(job_data.get("source", "") or "unknown", "dropped")
            logger.error(
                "job_fetcher: dropped a job from %s — %s at %s (%s): %s",
                job_data.get("source") or "?", job_data.get("title") or "?",
                job_data.get("company") or "?", job_data.get("url") or "?", exc,
            )

    # Now, with the job loop finished and the commit one line away. Re-read
    # first: this cycle has been running for minutes and the agent poll, the
    # mailbox poller and a settings save all write this same blob — the copy
    # taken above is stale, and writing it wholesale would revert them.
    try:
        db.refresh(profile)
        fresh = copy.deepcopy(profile.data or {})
        for key in _FETCH_CYCLE_KEYS:
            if key in merged_data:
                fresh[key] = merged_data[key]
        profile.data = fresh
    except Exception as exc:
        # The jobs are what this cycle is for. Losing the cycle's own bookkeeping
        # is a bad trade against losing the batch.
        logger.error("job_fetcher: could not merge cycle state into profile: %s", exc)

    try:
        db.commit()
    except Exception as exc:
        logger.error("job_fetcher: DB commit failed: %s", exc)
        db.rollback()

    # Enrich what just arrived, before matching scores it. A job matched on
    # Adzuna's 500-character stub is filtered for "too few skills" and never
    # seen again, so the minutes between storing it and scoring it are the only
    # chance to give the matcher the real posting.
    #
    # The landing HTML from link resolution goes in with it: those pages were
    # downloaded moments ago and thrown away after slug mining, and the job
    # description is sitting in them.
    if settings.ENRICH_ENABLED and settings.ENRICH_ON_FETCH:
        try:
            from app.services.enrichment import run as enrich_run
            counts["enrichment"] = enrich_run(
                db,
                limit=settings.ENRICH_MAX_PER_FETCH,
                landing_html=(resolve_stats.landing_html if resolve_stats else None),
            )
        except Exception as exc:
            logger.error("job_fetcher: enrichment pass failed: %s", exc)
            db.rollback()

    counts["per_source"] = per_source
    _log_run_summary(counts, source_stats, per_source, resolve_stats, board_stats,
                     started_at)

    # History outlives the profile's single-run snapshot, so trends are visible.
    try:
        from app.services.fetch_history import record_run
        record_run(
            db,
            started_at=started_at,
            counts=counts,
            source_stats=source_stats,
            per_source_outcome=per_source,
            queries=queries,
            locations=locations,
            resolve_stats=resolve_stats.as_dict() if resolve_stats else None,
            board_stats=board_stats,
            backfill=backfill_report,
            group=group or "all",
        )
        db.commit()
    except Exception as exc:
        logger.error("job_fetcher: could not record run history: %s", exc)
        db.rollback()

    return counts


def _log_run_summary(counts: dict, source_stats: dict, per_source: dict,
                     resolve_stats, board_stats: dict, started_at: datetime) -> None:
    """
    One readable block per cycle, so the container log answers the same
    questions the UI does without needing the UI.
    """
    from app.services.source_diagnostics import classify

    elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
    logger.info(
        "=== fetch cycle done in %.1fs — fetched=%d new=%d merged=%d dup=%d "
        "stale=%d dropped=%d ===",
        elapsed, counts["fetched"], counts["inserted"], counts["merged"],
        counts["skipped"], counts["stale"], counts.get("dropped", 0),
    )
    logger.info("  %-16s %-9s %7s %6s %7s  %s",
                "SOURCE", "STATUS", "FETCHED", "NEW", "DUP", "REASON")
    for source, stats in sorted(source_stats.items()):
        outcome = per_source.get(source, {})
        reason = (stats.get("errors") or [""])[0]
        logger.info(
            "  %-16s %-9s %7d %6d %7d  %s",
            source, classify(stats), stats.get("count", 0),
            outcome.get("inserted", 0),
            outcome.get("skipped", 0) + outcome.get("merged", 0),
            reason[:120],
        )
    if resolve_stats:
        logger.info("  apply links: %s", resolve_stats.as_dict())
    if board_stats:
        registry = board_stats.get("registry") or {}
        logger.info(
            "  boards: %d discovered, %d sniffed, active per ATS %s",
            board_stats.get("discovered", 0) or 0,
            board_stats.get("sniffed", 0) or 0,
            {ats: info.get("active", 0) for ats, info in registry.items()},
        )
