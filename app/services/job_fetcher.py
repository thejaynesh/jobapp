import asyncio
import logging
import time
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import live, settings
from app.models.job import Job, JobStatus
from app.models.profile import Profile
from app.services import posting_identity
from app.services.deduplication import (
    KnownPostings, compute_dedupe_hash, enrich_from, find_existing_job, ids_by_each_address,
    merge_description, merge_or_skip, note_addresses, note_source, was_archived,
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
        # Metered, so it sits out the API runs inside its own interval — see
        # `_sources_not_due`.
        "google_jobs",
        # SimplifyJobs' curated early-career postings: two files, one request
        # each.
        "simplify",
        # Amazon's own careers search (search.json), full descriptions inline.
        "amazon",
        # TikTok's own careers search, likewise.
        "tiktok",
        # Apple's careers search: server-rendered pages, details for matches.
        "apple",
        # Dice answers a plain HTTP request through its search API now, so it
        # left the browser tier — see `sources.dice.fetch_api`.
        "dice",
    }),
    # The company board registry: hundreds of slugs, one request each.
    "boards": frozenset({
        "greenhouse", "lever", "ashby", "smartrecruiters", "workable",
        "recruitee", "workday", "icims", "bamboohr", "teamtailor", "jobvite",
        "personio",
        # Large employers' careers platforms, read by careers host.
        "oracle", "successfactors", "phenom", "eightfold", "jibe",
        "rippling", "pinpoint",
        # JazzHR: its sitemaps name every open posting, so no registry needed.
        "jazzhr",
        "taleo", "paylocity",
        # Avature: each portal's sitemap lists its every open posting.
        "avature",
    }),
    # Playwright. The expensive tier, and the one worth running least often.
    "browser": frozenset({"wellfound", "handshake"}),
}

ALL_GROUPS = tuple(SOURCE_GROUPS)

# Board adapters that list a company's whole board in one response, and say
# so (`sources.base.saw_postings`). A stored posting such a read no longer
# lists has gone from the board, and is closed then rather than whenever the
# liveness sweep reaches it. The searched and capped boards (Workday, Oracle,
# SmartRecruiters' first hundred…) list a slice, and say nothing about the rest.
FULL_FEED_BOARDS = frozenset({
    "greenhouse", "lever", "ashby", "recruitee", "pinpoint", "paylocity",
    "bamboohr", "personio", "workable", "avature",
})
VANISHED_NOTE = "no longer listed on its board"

# Board adapters that take the cycle's role queries.
_SEARCHED_BOARDS = frozenset({
    "oracle", "successfactors", "phenom", "eightfold", "jibe", "rippling", "taleo",
    "avature", "smartrecruiters",
})


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
    # When this source last reported, for its duration (see `_run_all_adapters`).
    entry["_last"] = time.monotonic()
    if jobs and any(not j.get("_ingested_outcome") for j in jobs):
        from app.services.sources.base import BoardResult, publish_batch
        publish_batch(source, "", BoardResult(jobs=jobs, error=error))


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


def _fetch_dice(stats: dict, roles: list[str], locations: list[str]) -> list[dict]:
    """
    Dice through its search API; the browser scrape only if the API refuses.

    The API is the same one Dice's own page calls, so it returns structured
    rows — employer, pay, posting date — for a plain request, where the scrape
    needed Chromium and got cards with none of that. The key it needs is a
    public one that could rotate; when the API refuses, the scrape still runs
    so Dice does not go dark while the setting is updated.
    """
    from app.services.sources.dice import DiceApiUnavailable, fetch_api

    jobs: list[dict] = []
    refused: str | None = None
    for role in roles:
        for loc in locations:
            try:
                found = fetch_api(role, loc)
            except DiceApiUnavailable as exc:
                refused = str(exc)
                break
            except Exception as exc:
                _record(stats, "dice", [], f"{role}/{loc}: {exc}")
                continue
            _record(stats, "dice", found)
            jobs.extend(found)
        if refused:
            break
    if not refused:
        return jobs

    _record(stats, "dice", [], f"search API unavailable ({refused}); used the browser scrape")
    logger.error("Dice: search API unavailable (%s); falling back to the browser scrape",
                 refused)
    from app.services.sources.dice import fetch as dice_scrape

    async def _scrape_all() -> list[dict]:
        out: list[dict] = []
        for role in roles:
            for loc in locations:
                try:
                    found = await dice_scrape(query=role, location=loc)
                except Exception as exc:
                    _record(stats, "dice", [], f"{role}/{loc}: {exc}")
                    continue
                _record(stats, "dice", found)
                out.extend(found)
        return out

    try:
        jobs.extend(asyncio.run(_scrape_all()))
    except Exception as exc:
        _record(stats, "dice", [], f"browser scrape failed: {exc}")
    return jobs


def _run_all_adapters(
    roles: list[str], locations: list[str], cfg,
    ats_slugs: dict | None = None, loc_prefs: dict | None = None,
    only: set[str] | None = None, resting: dict | None = None,
    manual: bool | None = None, not_due: dict | None = None,
) -> tuple[list[dict], dict]:
    """
    `_run_adapters` under this cycle's settings.

    Adapters read most values off `cfg`, but the board adapters each ask
    `sources.base.board_workers()` for their concurrency on their own; this
    is what makes that answer the settings page's rather than the env's.
    """
    from app.services.sources.base import cycle_settings

    try:
        workers = max(1, int(getattr(cfg, "FETCH_SOURCE_CONCURRENCY", 1) or 1))
    except (TypeError, ValueError):
        workers = 1
    with cycle_settings(cfg):
        if workers == 1:
            return _run_adapters(
                roles, locations, cfg, ats_slugs, loc_prefs, only=only,
                resting=resting, manual=manual, not_due=not_due,
            )
        return _run_in_lanes(
            workers, roles, locations, cfg, ats_slugs, loc_prefs, only=only,
            resting=resting, manual=manual, not_due=not_due,
        )


# The browser tier's sources share one Chromium launch, so they share a lane.
_SHARED_LANES = (frozenset({"wellfound", "handshake"}),)
# Started first, since a cycle can end no sooner than its longest source: the
# board families with hundreds of sites each, then the searches that page.
_SLOW_FIRST = (
    "workday", "greenhouse", "jazzhr", "oracle", "avature", "successfactors",
    "eightfold", "phenom", "icims", "smartrecruiters", "bamboohr", "lever",
    "ashby", "linkedin", "simplify", "apple", "amazon", "tiktok", "builtin",
)


def _run_in_lanes(
    workers: int, roles: list[str], locations: list[str], cfg,
    ats_slugs: dict | None = None, loc_prefs: dict | None = None,
    only: set[str] | None = None, resting: dict | None = None,
    manual: bool | None = None, not_due: dict | None = None,
) -> tuple[list[dict], dict]:
    """
    `_run_adapters`, one source per lane, `workers` lanes at a time.

    The sources were read one after another, so a board cycle took the sum of
    every family's time — four hours on average — although each reads its own
    hosts and none waits on another. Each lane is a whole `_run_adapters`
    restricted to its sources, so every source runs exactly the code it ran
    before; only the waiting is shared.

    The cycle's context — its settings overlay, the stored descriptions, the
    board sightings being collected — is copied into each lane's thread, which
    would otherwise see none of it. The jobs are put back in the order the
    sequential run produced them, so which source first stores a posting does
    not come down to which lane finished first.
    """
    import contextvars
    from concurrent.futures import ThreadPoolExecutor

    from app.services.ats_discovery import build_ats_slugs

    if ats_slugs is None:
        ats_slugs = build_ats_slugs(cfg)
    if manual is None:
        manual = only is not None
    # Every source there is, in the order the sequential run reaches them:
    # asked of `_run_adapters` itself with nothing selected, which calls
    # nothing and so stays in step with it by construction.
    _, catalogue = _run_adapters(roles, locations, cfg, ats_slugs, loc_prefs, only=set(),
                                 manual=manual, reset_caches=False, log_summary=False)
    order = list(catalogue)
    wanted = [s for s in order if only is None or s in only]

    lanes: list[set[str]] = []
    for shared in _SHARED_LANES:
        together = {s for s in wanted if s in shared}
        if together:
            lanes.append(together)
    lanes += [{s} for s in wanted if not any(s in shared for shared in _SHARED_LANES)]
    lanes.sort(key=lambda lane: min(
        (_SLOW_FIRST.index(s) if s in _SLOW_FIRST else len(_SLOW_FIRST)) for s in lane))

    _reset_source_caches()

    def lane_run(lane: set[str]):
        return _run_adapters(roles, locations, cfg, ats_slugs, loc_prefs, only=lane,
                             resting=resting, manual=manual, not_due=not_due,
                             reset_caches=False, log_summary=False)

    with ThreadPoolExecutor(max_workers=min(workers, max(1, len(lanes))),
                            thread_name_prefix="source") as pool:
        futures = [(lane, pool.submit(contextvars.copy_context().run, lane_run, lane))
                   for lane in lanes]
        results = []
        for lane, future in futures:
            try:
                results.append((lane, *future.result()))
            except Exception as exc:
                # `_run_adapters` records its sources' failures itself; this is
                # the lane dying outright, which must not take the others with it.
                logger.error("fetch: sources %s failed: %s", sorted(lane), exc)
                results.append((lane, [], {s: {"count": 0, "errors": [str(exc)],
                                               "enabled": True} for s in lane}))

    rank = {source: i for i, source in enumerate(order)}
    results.sort(key=lambda result: min(rank.get(s, len(rank)) for s in result[0]))
    all_jobs: list[dict] = []
    stats: dict = {}
    for lane, jobs, lane_stats in results:
        all_jobs.extend(jobs)
        for source, entry in lane_stats.items():
            # Each lane reports every other source as not run; the lane that
            # ran a source is the one that knows about it.
            if source in lane or source not in stats:
                stats[source] = entry
    stats = {s: stats[s] for s in sorted(stats, key=lambda s: rank.get(s, len(rank)))}
    _log_fetch_summary(stats)
    return all_jobs, stats


def _run_adapters(
    roles: list[str], locations: list[str], cfg,
    ats_slugs: dict | None = None, loc_prefs: dict | None = None,
    only: set[str] | None = None, resting: dict | None = None,
    manual: bool | None = None, not_due: dict | None = None,
    reset_caches: bool = True, log_summary: bool = True,
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
    # results and looks like the change did nothing. (Run in lanes, the caller
    # resets them once, rather than each lane clearing another's mid-use.)
    if reset_caches:
        _reset_source_caches()

    resting = resting or {}
    # Sources that ran recently enough to sit this cycle out: {source: why}.
    not_due = not_due or {}
    started: dict[str, float] = {}
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
        # A metered source inside its own interval. Also not on a manual run,
        # for the same reason as resting: asking for it by name is the check.
        if not manual and source in not_due:
            _disable(source, not_due[source])
            return True
        # Every source asks this immediately before it starts, which makes it
        # the one place to start a clock without touching thirty branches.
        if source not in started:
            started[source] = time.monotonic()
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

    # --- Google Jobs: Google's job results, through SerpApi (metered) ---
    if (getattr(cfg, "SERPAPI_API_KEY", "") and getattr(cfg, "GOOGLE_JOBS_ENABLED", True)
            and not _skip("google_jobs")):
        from app.services.sources.google_jobs import fetch_all as google_jobs_fetch
        stats.setdefault("google_jobs", {"count": 0, "errors": [], "enabled": True})
        try:
            jobs = google_jobs_fetch(
                api_key=cfg.SERPAPI_API_KEY, queries=roles, locations=locations,
                max_searches=getattr(cfg, "GOOGLE_JOBS_MAX_SEARCHES", 8),
                pages=getattr(cfg, "GOOGLE_JOBS_PAGES", 1),
            )
            _record(stats, "google_jobs", jobs)
            all_jobs.extend(jobs)
        except Exception as exc:
            _record(stats, "google_jobs", [], str(exc))
    else:
        _disable("google_jobs")

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
        ("oracle", "app.services.sources.oracle"),
        ("successfactors", "app.services.sources.successfactors"),
        ("phenom", "app.services.sources.phenom"),
        ("eightfold", "app.services.sources.eightfold"),
        ("jibe", "app.services.sources.jibe"),
        ("rippling", "app.services.sources.rippling"),
        ("pinpoint", "app.services.sources.pinpoint"),
        ("taleo", "app.services.sources.taleo"),
        ("paylocity", "app.services.sources.paylocity"),
        ("avature", "app.services.sources.avature"),
    ):
        slugs = ats_slugs.get(ats_name) or []
        if slugs and not _skip(ats_name):
            import importlib
            ats_fetch = importlib.import_module(fetch_path).fetch
            stats.setdefault(ats_name, {"count": 0, "errors": [], "enabled": True})
            try:
                # The large-employer platforms search, or gate a whole feed,
                # by the roles; the rest return a company's every opening.
                jobs = (ats_fetch(company_slugs=slugs, queries=roles)
                        if ats_name in _SEARCHED_BOARDS else ats_fetch(company_slugs=slugs))
                _record(stats, ats_name, jobs)
                all_jobs.extend(jobs)
            except Exception as exc:
                _record(stats, ats_name, [], str(exc))
        else:
            _disable(ats_name)

    # --- JazzHR: companies with new postings matching the roles, from its sitemaps ---
    if getattr(cfg, "JAZZHR_ENABLED", True) and roles and not _skip("jazzhr"):
        from app.services.sources.jazzhr import fetch as jazzhr_fetch
        stats.setdefault("jazzhr", {"count": 0, "errors": [], "enabled": True})
        try:
            jobs = jazzhr_fetch(queries=roles,
                                max_companies=getattr(cfg, "JAZZHR_MAX_COMPANIES", None))
            _record(stats, "jazzhr", jobs)
            all_jobs.extend(jobs)
        except Exception as exc:
            _record(stats, "jazzhr", [], str(exc))
    else:
        _disable("jazzhr")

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
                    lambda role: builtin_fetch(
                        query=role, max_pages=getattr(cfg, "BUILTIN_MAX_PAGES", None)),
                    [(r,) for r in roles], _skip)
    else:
        _disable("builtin")

    # --- Amazon: its own careers search, per role and country ---
    if getattr(cfg, "AMAZON_ENABLED", True) and not _skip("amazon"):
        from app.services.sources.amazon import countries_for
        from app.services.sources.amazon import fetch as amazon_fetch
        _run_combos(
            stats, all_jobs, "amazon",
            lambda role, country: amazon_fetch(
                query=role, country=country,
                max_pages=getattr(cfg, "AMAZON_MAX_PAGES", None)),
            [(r, c) for r in roles for c in countries_for(adzuna_country_codes)],
            _skip,
        )
    else:
        _disable("amazon")

    # --- TikTok: its own careers search, per role, in the profile's countries ---
    if getattr(cfg, "TIKTOK_ENABLED", True) and not _skip("tiktok"):
        from app.services.sources import tiktok
        try:
            # One request for the cities TikTok hires in; every search is then
            # restricted to those in the profile's countries.
            cities = tiktok.city_codes(adzuna_country_codes)
        except Exception as exc:
            stats.setdefault("tiktok", {"count": 0, "errors": [], "enabled": True})
            _record(stats, "tiktok", [], f"city list: {exc}")
            cities = []
        _run_combos(
            stats, all_jobs, "tiktok",
            lambda role: tiktok.fetch(query=role, cities=cities,
                                      max_pages=getattr(cfg, "TIKTOK_MAX_PAGES", None)),
            [(r,) for r in roles] if cities else [],
            _skip,
        )
    else:
        _disable("tiktok")

    # --- Apple: its own careers search, per role and country ---
    if getattr(cfg, "APPLE_ENABLED", True) and not _skip("apple"):
        from app.services.sources import apple
        _run_combos(
            stats, all_jobs, "apple",
            lambda role, location: apple.fetch(
                query=role, location=location,
                max_pages=getattr(cfg, "APPLE_MAX_PAGES", None),
                max_details=getattr(cfg, "APPLE_MAX_DETAILS", None)),
            [(r, c) for r in roles for c in apple.countries_for(adzuna_country_codes)],
            _skip,
        )
    else:
        _disable("apple")

    # --- SimplifyJobs: curated US early-career postings, one file per list ---
    simplify_urls = [
        u.strip() for u in str(getattr(cfg, "SIMPLIFY_LISTINGS_URLS", "") or "").split(",")
        if u.strip()
    ]
    if getattr(cfg, "SIMPLIFY_ENABLED", True) and simplify_urls and not _skip("simplify"):
        from app.services.sources.simplify import fetch as simplify_fetch
        stats.setdefault("simplify", {"count": 0, "errors": [], "enabled": True})
        try:
            jobs = simplify_fetch(simplify_urls,
                                  max_age_days=getattr(cfg, "MAX_JOB_AGE_DAYS", None))
            _record(stats, "simplify", jobs)
            all_jobs.extend(jobs)
        except Exception as exc:
            _record(stats, "simplify", [], str(exc))
    else:
        _disable("simplify")

    # --- Jobspresso: curated remote jobs RSS feed ---
    from app.services.sources.jobspresso import fetch as jobspresso_fetch
    _run_combos(stats, all_jobs, "jobspresso",
                lambda role: jobspresso_fetch(query=role), [(r,) for r in roles], _skip)

    # --- Dice: its own search API, with the browser scrape as the fallback ---
    if getattr(cfg, "DICE_ENABLED", True) and not _skip("dice"):
        stats.setdefault("dice", {"count": 0, "errors": [], "enabled": True})
        all_jobs.extend(_fetch_dice(stats, roles, locations))
    else:
        _disable("dice")

    # --- Tier 2: Playwright scrapers (Wellfound, Handshake) ---
    # Launching a browser is the most expensive thing here, so don't do it at
    # all when none of its sources were asked for.
    pw_sources = {"wellfound", "handshake"}
    # Resting applies here too; these branches never went through `_skip`.
    pw_rested = {src for src in pw_sources if _rests(src)}
    wanted_pw = (pw_sources if only is None else pw_sources & only) - pw_rested
    # Nor for one that is switched off or unconfigured: launching Chromium to
    # find that out inside the tier is the most expensive way to learn it.
    if not getattr(cfg, "WELLFOUND_ENABLED", True):
        wanted_pw.discard("wellfound")
    if not getattr(cfg, "HANDSHAKE_SESSION_COOKIE", ""):
        wanted_pw.discard("handshake")
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
            from app.services.sources.wellfound import configured_roles as wf_configured_roles
            from app.services.sources.wellfound import fetch_roles as wf_fetch_roles
            pw_stats.setdefault("wellfound", {"count": 0, "errors": [], "enabled": True})
            try:
                jobs = await wf_fetch_roles(
                    slugs=wf_configured_roles(cfg),
                    location=locations[0] if locations else "",
                )
                _record(pw_stats, "wellfound", jobs)
                pw_jobs.extend(jobs)
            except Exception as exc:
                _record(pw_stats, "wellfound", [], str(exc))
        else:
            pw_stats["wellfound"] = {"count": 0, "errors": [], "enabled": False}

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
        for src in ("wellfound", "handshake"):
            stats.setdefault(src, {"count": 0, "errors": [str(exc)], "enabled": True})
    for src in pw_rested:
        stats[src] = {"count": 0, "enabled": False, "errors": [_resting_reason(src)]}

    # How long each source took. Board runs average four hours and nothing
    # said where the time went; this is the number that answers it.
    for source, entry in stats.items():
        last = entry.pop("_last", None)
        if source in started and last is not None:
            entry["seconds"] = round(max(0.0, last - started[source]), 1)

    if log_summary:
        _log_fetch_summary(stats)
    return all_jobs, stats


def _log_fetch_summary(stats: dict) -> None:
    logger.info("=== fetch summary ===")
    for source, s in stats.items():
        status = "disabled" if not s["enabled"] else (
            f"OK {s['count']} jobs" if not s["errors"] else
            f"PARTIAL {s['count']} jobs, {len(s['errors'])} error(s)"
            if s["count"] > 0 else
            f"FAILED {len(s['errors'])} error(s)"
        )
        took = f" in {s['seconds']:g}s" if s.get("seconds") is not None else ""
        logger.info("  %-12s %s%s", source, status, took)
        for err in s["errors"]:
            logger.warning("    └─ %s", err)


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
            threshold=live().SOURCE_REST_AFTER_FAILURES,
            retry_every=live().SOURCE_REST_RETRY_EVERY,
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


# Metered sources and the setting holding each one's minimum gap between runs.
# The API group runs every couple of hours, which is right for a free feed and
# ruinous for a search that spends a monthly quota: at the default cadence,
# eight Google Jobs searches a run would spend a free SerpApi month in three
# days.
_MIN_INTERVAL_HOURS = {"google_jobs": "GOOGLE_JOBS_INTERVAL_HOURS"}


def _sources_not_due(db: Session, cfg) -> dict[str, str]:
    """
    Metered sources that ran too recently to run again: {source: reason}.

    Never fails the cycle. Not knowing when a source last ran costs one early
    run of it, which is cheaper than losing the fetch to a history query.
    """
    from datetime import timedelta

    from app.services.fetch_history import last_attempted

    waiting: dict[str, str] = {}
    now = datetime.now(timezone.utc)
    for source, setting in _MIN_INTERVAL_HOURS.items():
        try:
            hours = float(getattr(cfg, setting, 0) or 0)
            if hours <= 0:
                continue
            last = last_attempted(db, source)
        except Exception as exc:
            logger.warning("job_fetcher: could not read when %s last ran: %s", source, exc)
            continue
        if last is None:
            continue
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        due = last + timedelta(hours=hours)
        if due > now:
            wait = max(1, round((due - now).total_seconds() / 3600))
            waiting[source] = (
                f"ran {max(0, round((now - last).total_seconds() / 3600))}h ago; "
                f"runs at most every {hours:g}h to spare its quota, next in "
                f"about {wait}h (a manual run ignores this)"
            )
    try:
        from app.services.capacity import source_waits
        for source, reason in source_waits(db, cfg, now).items():
            waiting.setdefault(source, reason)
    except Exception as exc:
        logger.warning("job_fetcher: adaptive source history unavailable: %s", exc)
    return waiting


_LOOKUP_CHUNK = _COMMIT_EVERY


def _known_postings(db: Session, chunk: list[dict]):
    """
    The stored and archived rows this chunk of postings matches, looked up in
    one pass, and the matched rows themselves loaded in one query. Loaded
    whole: deferring the descriptions made every merge that compares text
    fetch its row's description on its own, and the chunk ran slower than the
    per-posting lookups it replaced (11 s against 8.3 s for 3,000). None when
    the lookup fails; the loop then asks per posting, as it always did.
    """
    postings = []
    for j in chunk:
        try:
            postings.append(
                {"url": j.get("url", ""), "apply_url": j.get("apply_url"),
                 "source": j.get("source", ""), "source_job_id": j.get("source_job_id"),
                 "dedupe_hash": compute_dedupe_hash(j.get("company", ""), j.get("title", ""),
                                                    j.get("location", ""), j.get("url", ""))})
        except Exception:
            # A posting that cannot even be hashed fails again in the loop,
            # inside its own savepoint, where it is counted and named.
            continue
    try:
        # Its own savepoint: a failed read must not roll back the inserts of
        # the chunks before it that have not been committed yet.
        with db.begin_nested():
            known = KnownPostings(db, postings)
            ids = set(known.by_address.values()) | set(known.by_pair.values()) \
                | {job_id for job_id in known.by_listing.values() if job_id is not None}
            # Held on `known` for the chunk: the session keeps only weak
            # references, and an unreferenced row would be read again by `db.get`.
            known.rows = db.query(Job).filter(
                Job.id.in_(list(ids))).order_by(Job.id).with_for_update().all() if ids else []
        return known
    except Exception as exc:
        logger.warning("job_fetcher: batched lookup failed, asking per posting: %s", exc)
        return None


def _known_urls(db: Session, urls) -> set[str]:
    """
    Which of these URLs are already on a stored job, as its listing, its apply
    link or one of its sightings.

    Asked about the candidates only. This used to read every URL of every
    stored job into a set on every cycle — the whole table, arrays and all,
    to answer a question about the handful of interstitial links a cycle
    brings in. Each of the three lookups has an index (0038); for
    `source_urls`, the candidates joined one by one against the GIN index is
    the form it answers at any size (`deduplication.ids_by_each_address`).
    """
    wanted = [u for u in dict.fromkeys(urls) if u]
    known: set[str] = set()
    for start in range(0, len(wanted), 1000):
        chunk = wanted[start:start + 1000]
        known.update(u for (u,) in db.query(Job.url).filter(Job.url.in_(chunk)))
        known.update(u for (u,) in db.query(Job.apply_url).filter(Job.apply_url.in_(chunk)))
        known.update(ids_by_each_address(db, Job, chunk))
    return known


def _resolve_apply_links(db: Session, raw_jobs: list[dict]):
    """
    Turn aggregator interstitials into real apply URLs, in place.

    Restricted to postings we've never stored, so steady-state cycles spend
    almost no requests here — a job's apply link is resolved exactly once.
    """
    from app.services.link_resolver import is_interstitial, resolve_jobs

    candidates = [job for job in raw_jobs if is_interstitial(job.get("url") or "")]
    known = _known_urls(db, {job.get("url") or "" for job in candidates})
    fresh = [job for job in candidates if (job.get("url") or "") not in known]
    if not fresh:
        return None
    return resolve_jobs(
        fresh,
        max_links=live().LINK_RESOLVE_MAX_PER_CYCLE,
        workers=live().LINK_RESOLVE_WORKERS,
        per_host=live().LINK_RESOLVE_PER_HOST,
        host_delay=live().LINK_RESOLVE_HOST_DELAY_MS / 1000.0,
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

    if not live().BOARD_BACKFILL_ON_START:
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
                max_links=live().BOARD_BACKFILL_MAX_LINKS,
                max_hosts=live().BOARD_BACKFILL_MAX_HOSTS,
                workers=live().BOARD_BACKFILL_WORKERS,
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
    career_links: dict | None = None,
    board_results: dict | None = None,
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
    from app.services.tunables import value as tunable_value
    if tunable_value(updated_data, "ats_sniff_career_sites"):
        stats["sniffed"] = _sniff_career_sites(db, raw_jobs, resolve_stats, updated_data,
                                               career_links)

    # Per-board yield, so next cycle's budget favours boards that produce.
    for ats, attempted in (ats_slugs or {}).items():
        attempted = [slug for slug in attempted if not getattr((board_results or {}).get((ats, slug)), "recorded", False)]
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
            max_empty_cycles=live().ATS_BOARD_MAX_EMPTY_CYCLES,
            results={slug: value for (source, slug), value in (board_results or {}).items() if source == ats and slug},
        )

    return stats


def _sniff_career_sites(db: Session, raw_jobs: list[dict], resolve_stats,
                        updated_data: dict, career_links: dict | None = None) -> int:
    """
    Mine company careers sites for the ATS board behind them: the sites this
    cycle's postings link to, then those the community lists name
    (`career_links`, host → a posting there and its company).
    """
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
    hints: dict[str, dict] = {}  # host → a posting there, for the sniffer to read
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
        if host not in hints or ("gh_jid=" in candidate and "gh_jid=" not in hints[host]["url"]):
            hints[host] = {"url": candidate, "company": job.get("company") or ""}

    for host, link in (career_links or {}).items():
        if is_aggregator(link.get("url") or ""):
            continue
        hosts.setdefault(host, "")
        hints.setdefault(host, link)
        if link.get("company"):
            host_company.setdefault(host, link["company"])

    if not hosts:
        return 0

    from app.services.tunables import value as tunable_value
    merged, cache, per_host = sniff_hosts(
        hosts,
        updated_data.get("ats_sniff_cache"),
        max_hosts=int(tunable_value(updated_data, "ats_sniff_max_hosts_per_cycle") or 0),
        hints=hints,
    )
    updated_data["ats_sniff_cache"] = cache

    new_boards = 0
    for host, found in per_host.items():
        new_boards += boards.record_boards(
            db, found, origin="sniffed",
            company=host_company.get(host), source_host=host,
        )
    return new_boards


def fetch_and_save_jobs(db: Session, only: set[str] | None = None, group: str | None = None) -> dict:
    """Durable run lifecycle, including failures before the first job arrives."""
    from app.models.fetch_run import FetchRun
    # Called under the group's lease. Prior unfinished runs lost their worker;
    # their completed batches remain committed and pending batches replay below.
    db.query(FetchRun).filter(FetchRun.group == (group or "all"), FetchRun.status == "running").update(
        {FetchRun.status: "partial", FetchRun.finished_at: datetime.now(timezone.utc),
         FetchRun.error: "Worker interrupted; completed batches preserved, pending batches replayed on recovery"},
        synchronize_session=False)
    run = FetchRun(started_at=datetime.now(timezone.utc), group=group or "all", status="running")
    db.add(run)
    db.commit()
    run_id = run.id
    try:
        result = _fetch_and_save_jobs(db, only, group, run_id=run_id)
        db.expire_all()
        saved = db.get(FetchRun, run_id)
        if saved and saved.finished_at is None:
            saved.finished_at = datetime.now(timezone.utc)
            saved.status = "failed" if result.get("error") else "ok"
            saved.error = result.get("error")
            db.commit()
        return result
    except Exception as exc:
        db.rollback()
        saved = db.get(FetchRun, run_id)
        if saved:
            saved.finished_at = datetime.now(timezone.utc)
            saved.status = "failed"
            saved.error = str(exc)[:2000]
            db.commit()
        raise


def _fetch_and_save_jobs(
    db: Session, only: set[str] | None = None, group: str | None = None,
    run_id=None,
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
              "dropped": 0, "closed": 0, "sources": {}, "group": group or "all"}

    profile = db.query(Profile).first()
    if not profile:
        logger.warning("job_fetcher: no profile found, skipping.")
        return counts

    roles: list[str] = profile.data.get("target_roles") or []

    # The settings page's overrides on top of the environment: what every read
    # below sees, and what the adapters are handed (`tunables.effective_settings`).
    from app.services.tunables import effective_settings
    cfg = effective_settings(profile.data)

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
            settings.NVIDIA_NIM_BASE_URL, cfg.NVIDIA_NIM_MODEL,
        )
    except Exception as exc:
        logger.error("job_fetcher: query expansion failed: %s", exc)
        queries = list(roles)
    if not queries:
        queries = list(roles)

    discovered_ats = (
        profile.data.get("discovered_ats") if cfg.ATS_AUTO_DISCOVERY else None
    )

    # Company boards named by community job lists (SimplifyJobs' listings
    # files, the new-grad READMEs). Every one goes to the board registry, which
    # probes a board before polling it — so the lists are read whole. They used
    # to be merged into the profile's discovered set, capped at 100 boards per
    # ATS and fifteen for Workday, which a single list filled on its first read
    # and nothing could ever add to again.
    #
    # Only on a cycle that polls boards: nothing else reads the registry, and
    # the lists run to tens of megabytes, which every two-hourly API run was
    # downloading to no purpose.
    from app.services.tunables import value as tunable_value
    harvested: dict = {}
    harvested_names: dict = {}
    # Employer-site links the lists name that no pattern recognised, for the
    # career-site sniffer to look behind (see `ats_sniffer`).
    harvested_links: dict = {}
    polls_boards = only is None or bool(set(only) & SOURCE_GROUPS["boards"])
    harvest_urls = [
        u.strip() for u in str(tunable_value(profile.data, "slug_harvest_urls") or "").split(",")
        if u.strip()
    ]
    if cfg.ATS_LIST_HARVEST and harvest_urls and polls_boards:
        try:
            from app.services.ats_discovery import _merge_found, harvest_boards_from_lists
            harvested, harvested_names = harvest_boards_from_lists(
                harvest_urls, career_links=harvested_links)
            if not cfg.ATS_BOARD_REGISTRY:
                # No registry to validate them: the capped legacy merge.
                merged = {ats: list(s or []) for ats, s in (discovered_ats or {}).items()}
                _merge_found(merged, harvested)
                discovered_ats = merged
        except Exception as exc:
            logger.error("job_fetcher: board harvest failed: %s", exc)

    # Validate/auto-fix the configured ATS slugs (cached per slug on the profile),
    # then assemble the final slug map: configured + verified seeds + discovered.
    from app.services.ats_discovery import build_ats_slugs, configured_ats_slugs, slug_caps
    # The settings page's overrides, for the board budget below as much as for
    # the adapters: the caps and the concurrency are preferences, not facts
    # about the deployment.
    cycle_cfg = cfg
    slug_cache = None
    slug_report: dict = {}
    validated_configured = None
    if cfg.ATS_SLUG_VALIDATION:
        try:
            from app.services.ats_validation import validate_configured_slugs
            validated_configured, slug_cache, slug_report = validate_configured_slugs(
                configured_ats_slugs(cfg), profile.data.get("ats_slug_cache")
            )
        except Exception as exc:
            logger.error("job_fetcher: slug validation failed: %s", exc)

    # The board registry is the durable store of every company ATS board we've
    # learned about, ranked by what each one actually yields. Legacy slugs from
    # the old profile blob are folded in on the way past.
    registry_boards = None
    backfill_report = None
    if cfg.ATS_BOARD_REGISTRY:
        try:
            from app.services import company_boards as boards
            # Savepoint, not the whole transaction: a registry problem must not
            # discard the query cache or the jobs this cycle is about to save.
            with db.begin_nested():
                if discovered_ats:
                    boards.backfill_from_slugs(db, discovered_ats, origin="discovered")
                if harvested:
                    # Replayed every cycle, so it must not revive what the
                    # registry retired — see `record_boards`.
                    boards.record_boards(db, harvested, origin="list", revive=False,
                                         names=harvested_names)
                if cfg.ATS_SEED_COMPANIES:
                    from app.services.ats_seeds import SEED_ATS_SLUGS, SEED_BOARD_NAMES
                    boards.backfill_from_slugs(db, SEED_ATS_SLUGS, origin="seed",
                                               names=SEED_BOARD_NAMES)
                if validated_configured:
                    boards.backfill_from_slugs(db, validated_configured, origin="configured")
            db.commit()
            # Before picking this cycle's slugs, so anything the backfill
            # recovers from the back catalogue is fetched straight away.
            backfill_report = _maybe_backfill_boards(db, profile)
            # And before that selection too: a board nobody has confirmed
            # exists is not polled, so the per-ATS budget goes to companies
            # rather than to slugs scraped off an aggregator's own page.
            if cfg.ATS_BOARD_VALIDATION:
                try:
                    with db.begin_nested():
                        boards.validate_pending(
                            db,
                            limit=tunable_value(profile.data, "ats_board_validate_per_cycle"),
                            workers=cycle_cfg.ATS_BOARD_FETCH_WORKERS,
                        )
                    db.commit()
                except Exception as exc:
                    logger.error("job_fetcher: board validation failed: %s", exc)
                    db.rollback()
            registry_boards = boards.registry_slugs(db, slug_caps(cycle_cfg))
        except Exception as exc:
            logger.error("job_fetcher: board registry unavailable: %s", exc)
            registry_boards = None

    ats_slugs = build_ats_slugs(
        cycle_cfg, discovered_ats, validated_configured, registry_boards
    )

    # Adapters handle their own failures and return [], so the reason a source
    # produced nothing lives only in its log line. Capture those and attach them
    # to the stats, otherwise a blocked source is indistinguishable from a
    # search that genuinely had no matches.
    from app.services.source_diagnostics import SourceLogCapture, merge_into_stats
    try:
        # The overlay (`cfg`, above) is `settings` with the profile's UI
        # overrides on top, so every adapter picks them up through the `cfg.X`
        # reads it already does.
        from app.services.sources.base import collect_board_sightings, known_descriptions, collection_results
        from app.services.collection_batches import sink_for, replay
        from app.models.company_board import CompanyBoard
        counts["replayed"] = replay(db, max_age_days=cfg.MAX_JOB_AGE_DAYS)
        cursors = {(b.ats, b.slug): b.fetch_cursor for b in db.query(CompanyBoard)
                   .filter(CompanyBoard.fetch_cursor.isnot(None))}
        described = {}
        versions = {}
        if getattr(cfg, "GREENHOUSE_DESCRIPTIONS_ON_DEMAND", True) and \
                (only is None or "greenhouse" in only):
            described["greenhouse"] = _described_ids(db, "greenhouse")
            from app.models.source_listing import SourceListing
            versions["greenhouse"] = {r.external_id: r.upstream_updated_at for r in db.query(SourceListing)
                                      .filter(SourceListing.source == "greenhouse")}
        with SourceLogCapture() as capture, collect_board_sightings() as sightings, \
                known_descriptions(described, versions), collection_results(
                    sink_for(db, run_id, max_age_days=cfg.MAX_JOB_AGE_DAYS), cursors) as board_results:
            raw_jobs, source_stats = _run_all_adapters(
                queries, locations, cfg, ats_slugs, loc_prefs, only,
                resting=_resting_sources(db), manual=manual,
                not_due=_sources_not_due(db, cfg),
            )
        merge_into_stats(source_stats, capture.messages, capture.errors)
        for (source, board), result in board_results.items():
            if board and (result.complete is False or result.error):
                entry = source_stats.setdefault(source, {"count": 0, "errors": [], "enabled": True})
                entry["incomplete"] = entry.get("incomplete", 0) + 1
                if result.error and result.error not in entry["errors"]:
                    entry["errors"].append(result.error[:300])
    except Exception as exc:
        logger.error("job_fetcher: _run_all_adapters failed: %s", exc)
        counts["error"] = str(exc)
        return counts

    counts["fetched"] = len(raw_jobs)
    counts["sources"] = source_stats
    now = datetime.now(timezone.utc)

    # Follow aggregator redirect pages through to the employer's own apply link.
    # Only postings we haven't seen before are worth the round trip.
    resolve_stats = None
    if cfg.RESOLVE_APPLY_LINKS:
        try:
            resolve_stats = _resolve_apply_links(db, raw_jobs)
        except Exception as exc:
            logger.error("job_fetcher: apply-link resolution failed: %s", exc)

    if cfg.ATS_BOARD_REGISTRY:
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
    if cfg.ATS_AUTO_DISCOVERY:
        try:
            from app.services.ats_discovery import discover_ats_slugs
            updated_data["discovered_ats"] = discover_ats_slugs(raw_jobs, discovered_ats)
        except Exception as exc:
            logger.error("job_fetcher: ATS discovery failed: %s", exc)

    board_stats: dict = {}
    if cfg.ATS_BOARD_REGISTRY:
        try:
            with db.begin_nested():
                board_stats = _update_board_registry(
                    db, raw_jobs, ats_slugs, source_stats,
                    resolve_stats, updated_data, career_links=harvested_links,
                    board_results=board_results,
                )
            db.commit()
            from app.services.company_boards import summary
            board_stats["registry"] = summary(db)
        except Exception as exc:
            logger.error("job_fetcher: board registry update failed: %s", exc)
            board_stats = {}

    # Hand what the server could not follow to the browser. Never blocks and
    # never fails the cycle: if no agent is listening the tasks simply expire.
    if cfg.RESOLVE_APPLY_LINKS:
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
                  "errors": s["errors"][:3],  # cap at 3 reasons stored
                  "seconds": s.get("seconds")}
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

    from app.services.collection_ingest import store as store_posting
    linked_jobs = []
    known = None
    for index, job_data in enumerate(raw_jobs):
        try:
            if (job_data.get("_ingested_outcome")
                    and job_data.get("apply_url") == job_data.get("_ingested_apply_url")
                    and job_data.get("company") == job_data.get("_ingested_company")):
                outcome = job_data["_ingested_outcome"]
                counts[outcome] += 1
                _tally(job_data.get("source") or "unknown", outcome)
                continue
            if known is None or index % _LOOKUP_CHUNK == 0:
                known = _known_postings(db, raw_jobs[index:index + _LOOKUP_CHUNK])
            outcome, job = store_posting(db, job_data, max_age_days=max_age_days, now=now, known=known)
            # Incremental ingestion already committed this sighting. Preserve
            # its original outcome while allowing resolved links/names to merge.
            prior = job_data.get("_ingested_outcome")
            if prior == "inserted" or (prior == "merged" and outcome == "skipped"):
                outcome = prior
            counts[outcome] += 1
            _tally(job_data.get("source") or "unknown", outcome)
            if job is not None:
                linked_jobs.append(job)
        except Exception as exc:
            counts["dropped"] += 1
            _tally(job_data.get("source") or "unknown", "dropped")
            logger.error("job_fetcher: dropped %s at %s (%s) from %s: %s",
                         job_data.get("title"), job_data.get("company"), job_data.get("url"), job_data.get("source"), exc)
        if (index + 1) % _COMMIT_EVERY == 0:
            from app.services.company_identity import attach_known_companies
            attach_known_companies(db, linked_jobs)
            db.commit()
            linked_jobs = []
    if linked_jobs:
        from app.services.company_identity import attach_known_companies
        attach_known_companies(db, linked_jobs)

    # Postings their board no longer lists. After the loop, so a posting that
    # moved (a new id for the same role) has had its new row stored first.
    try:
        with db.begin_nested():
            counts["closed"] = _close_vanished(db, sightings)
    except Exception as exc:
        logger.error("job_fetcher: closing vanished postings failed: %s", exc)

    # Now, with the job loop finished and the commit one line away. Re-read
    # first: this cycle has been running for minutes and the agent poll, the
    # mailbox poller and a settings save all write this same blob — the copy
    # taken above is stale, and writing it wholesale would revert them.
    try:
        db.refresh(profile, with_for_update=True)
        fresh = copy.deepcopy(profile.data or {})
        for key in _FETCH_CYCLE_KEYS:
            if key in merged_data:
                incoming = merged_data[key]
                if key == "discovered_ats":
                    merged = dict(fresh.get(key) or {})
                    for ats, slugs in (incoming or {}).items():
                        merged[ats] = list(dict.fromkeys([*(merged.get(ats) or []), *slugs]))
                    fresh[key] = merged
                elif key.endswith("_cache") or key == "ats_slug_report":
                    fresh[key] = {**(fresh.get(key) or {}), **(incoming or {})}
                else:
                    fresh[key] = incoming
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
        raise

    # Enrich what just arrived, before matching scores it. A job matched on
    # Adzuna's 500-character stub is filtered for "too few skills" and never
    # seen again, so the minutes between storing it and scoring it are the only
    # chance to give the matcher the real posting.
    #
    # The landing HTML from link resolution goes in with it: those pages were
    # downloaded moments ago and thrown away after slug mining, and the job
    # description is sitting in them.
    if cfg.ENRICH_ENABLED and cfg.ENRICH_ON_FETCH:
        try:
            from app.services.enrichment import run as enrich_run
            counts["enrichment"] = enrich_run(
                db,
                limit=cfg.ENRICH_MAX_PER_FETCH,
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
            run_id=run_id,
        )
        db.commit()
    except Exception as exc:
        logger.error("job_fetcher: could not record run history: %s", exc)
        db.rollback()

    return counts


# A stored description shorter than this is read again rather than trusted.
_DESCRIBED_MIN_CHARS = 200


def _described_ids(db: Session, source: str) -> set[str]:
    """
    The postings of `source` there is no need to download the text of again:
    stored with a real description, or archived (judged and retired; its text
    was thrown away on purpose, and the save skips it anyway).
    """
    from sqlalchemy import func

    from app.models.archived_job import ArchivedJob
    from app.models.source_listing import SourceListing
    from datetime import timedelta
    fresh_since = datetime.now(timezone.utc) - timedelta(hours=live().SOURCE_DETAIL_REFRESH_HOURS)

    stored = (
        db.query(Job.source_job_id)
        .join(SourceListing, SourceListing.job_id == Job.id)
        .filter(Job.source == source, Job.source_job_id.isnot(None),
                SourceListing.source == source, SourceListing.details_checked_at >= fresh_since,
                func.length(Job.description) >= _DESCRIBED_MIN_CHARS)
        .all()
    )
    archived = (
        db.query(ArchivedJob.source_job_id)
        .filter(ArchivedJob.source == source, ArchivedJob.source_job_id.isnot(None))
        .all()
    )
    return {row[0] for row in stored} | {row[0] for row in archived}


def _board_key(job_data: dict) -> str | None:
    """`source:slug` for a posting read from a full-feed board, else None."""
    source, slug = job_data.get("source"), job_data.get("ats_slug")
    if source in FULL_FEED_BOARDS and slug and job_data.get("source_job_id"):
        return f"{source}:{slug}"
    return None


def _note_board(existing: Job, job_data: dict) -> None:
    """
    File a stored posting under its board when this is that board's own
    sighting of it — same source, same id. A row stored from another source
    keeps that source's id, and comparing it against the board's would close it
    wrongly. A posting its board lists again is reopened if its board closed it.
    """
    key = _board_key(job_data)
    if not key or existing.source != job_data.get("source") \
            or existing.source_job_id != str(job_data.get("source_job_id")):
        return
    if existing.board is None:
        existing.board = key
    if existing.closed_at is not None and existing.closed_note == VANISHED_NOTE:
        existing.closed_at = None
        existing.closed_note = None


def _close_vanished(db: Session, sightings: dict) -> int:
    """
    Close the stored postings of each board read in full this cycle that the
    read no longer listed. Returns how many.

    Only boards that listed at least one posting: an empty answer is as likely
    a hiccup as a company that stopped hiring, and closing every posting on the
    strength of it is not a mistake worth risking — the liveness sweep checks
    those one by one.
    """
    listed = {
        f"{source}:{slug}": ids
        for (source, slug), ids in (sightings or {}).items()
        if source in FULL_FEED_BOARDS and ids
    }
    if not listed:
        return 0
    now = datetime.now(timezone.utc)
    closed = 0
    from app.models.source_listing import SourceListing
    affected = set()
    observed = set()
    for (source, slug), ids in (sightings or {}).items():
        if source not in FULL_FEED_BOARDS or not ids:
            continue
        for listing in db.query(SourceListing).filter(SourceListing.source == source,
                SourceListing.board == slug, SourceListing.job_id.isnot(None)):
            if listing.external_id not in ids:
                listing.closed_at = now
                affected.add(listing.job_id)
            else:
                listing.closed_at = None
                listing.last_seen_at = now
                observed.add(listing.job_id)
    if observed:
        db.query(Job).filter(Job.id.in_(observed)).update({Job.last_seen_at: now}, synchronize_session=False)
    db.flush()
    still_open = {row[0] for row in db.query(SourceListing.job_id).filter(
        SourceListing.job_id.in_(affected), SourceListing.source.in_(FULL_FEED_BOARDS),
        SourceListing.closed_at.is_(None))} if affected else set()
    for job in db.query(Job).filter(Job.id.in_(affected - still_open), Job.closed_at.is_(None)):
        job.closed_at, job.closed_note = now, VANISHED_NOTE
        closed += 1
    boards = sorted(listed)
    for start in range(0, len(boards), 500):
        rows = (
            db.query(Job)
            .filter(Job.closed_at.is_(None), Job.board.in_(boards[start:start + 500]))
            .all()
        )
        for job in rows:
            if job.closed_at is None and job.id not in (still_open | observed) and job.source_job_id and job.source_job_id not in listed[job.board]:
                job.closed_at = now
                job.closed_note = VANISHED_NOTE
                closed += 1
    if closed:
        logger.info("job_fetcher: closed %d postings their boards no longer list", closed)
    return closed


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
        "stale=%d dropped=%d closed=%d ===",
        elapsed, counts["fetched"], counts["inserted"], counts["merged"],
        counts["skipped"], counts["stale"], counts.get("dropped", 0),
        counts.get("closed", 0),
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
