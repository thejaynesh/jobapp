import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import settings
from app.models.job import Job, JobStatus
from app.models.profile import Profile
from app.services.deduplication import compute_dedupe_hash, find_existing_job, merge_or_skip

logger = logging.getLogger(__name__)

# Give the one-time board backfill a few shots at a flaky network, then stop.
_MAX_BACKFILL_ATTEMPTS = 3


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
    only: set[str] | None = None,
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

    def _skip(source: str) -> bool:
        """True when `only` excludes this source; records it as disabled."""
        if only is not None and source not in only:
            stats[source] = {"count": 0, "errors": [], "enabled": False}
            return True
        return False

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
        stats["adzuna"] = {"count": 0, "errors": [], "enabled": False}

    if cfg.JSEARCH_API_KEY and not _skip("jsearch"):
        from app.services.sources.jsearch import fetch as jsearch_fetch
        _run_combos(
            stats, all_jobs, "jsearch",
            lambda role, loc: jsearch_fetch(
                api_key=cfg.JSEARCH_API_KEY, query=role, location=loc),
            [(r, l) for r in roles for l in locations],
            _skip,
        )
    else:
        stats["jsearch"] = {"count": 0, "errors": [], "enabled": False}

    greenhouse_slugs = ats_slugs.get("greenhouse") or []
    if greenhouse_slugs and not _skip("greenhouse"):
        from app.services.sources.greenhouse import fetch as gh_fetch
        try:
            jobs = gh_fetch(company_slugs=greenhouse_slugs)
            _record(stats, "greenhouse", jobs)
            all_jobs.extend(jobs)
        except Exception as exc:
            _record(stats, "greenhouse", [], str(exc))
        stats.setdefault("greenhouse", {"count": 0, "errors": [], "enabled": True})
        stats["greenhouse"]["enabled"] = True
    else:
        stats["greenhouse"] = {"count": 0, "errors": [], "enabled": False}

    lever_slugs = ats_slugs.get("lever") or []
    if lever_slugs and not _skip("lever"):
        from app.services.sources.lever import fetch as lever_fetch
        try:
            jobs = lever_fetch(company_slugs=lever_slugs)
            _record(stats, "lever", jobs)
            all_jobs.extend(jobs)
        except Exception as exc:
            _record(stats, "lever", [], str(exc))
        stats.setdefault("lever", {"count": 0, "errors": [], "enabled": True})
        stats["lever"]["enabled"] = True
    else:
        stats["lever"] = {"count": 0, "errors": [], "enabled": False}

    ashby_slugs = ats_slugs.get("ashby") or []
    if ashby_slugs and not _skip("ashby"):
        from app.services.sources.ashby import fetch as ashby_fetch
        try:
            jobs = ashby_fetch(company_slugs=ashby_slugs)
            _record(stats, "ashby", jobs)
            all_jobs.extend(jobs)
        except Exception as exc:
            _record(stats, "ashby", [], str(exc))
        stats.setdefault("ashby", {"count": 0, "errors": [], "enabled": True})
        stats["ashby"]["enabled"] = True
    else:
        stats["ashby"] = {"count": 0, "errors": [], "enabled": False}

    # --- Additional slug-based ATS boards (no keys; slugs configured or auto-discovered) ---
    for ats_name, fetch_path in (
        ("smartrecruiters", "app.services.sources.smartrecruiters"),
        ("workable", "app.services.sources.workable"),
        ("recruitee", "app.services.sources.recruitee"),
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
            stats[ats_name] = {"count": 0, "errors": [], "enabled": False}

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
        stats["workday"] = {"count": 0, "errors": [], "enabled": False}

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
        stats["jooble"] = {"count": 0, "errors": [], "enabled": False}

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
        stats["careerjet"] = {"count": 0, "errors": [], "enabled": False}

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
        stats["findwork"] = {"count": 0, "errors": [], "enabled": False}

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
        stats["indeed"] = {
            "count": 0, "enabled": False,
            "errors": ["Indeed retired its public RSS feed (404 for every "
                       "query); set INDEED_RSS_ENABLED=true to retry it"],
        }

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

    # --- Tier 2: Playwright scrapers (Wellfound, Dice, Handshake) ---
    # Launching a browser is the most expensive thing here, so don't do it at
    # all when none of its sources were asked for.
    pw_sources = {"wellfound", "dice", "handshake"}
    run_browser_tier = only is None or bool(pw_sources & only)

    async def _run_playwright() -> tuple[list[dict], dict]:
        pw_jobs: list[dict] = []
        pw_stats: dict = {}

        if only is None or "wellfound" in only:
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

        if only is None or "dice" in only:
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

        if getattr(cfg, "HANDSHAKE_SESSION_COOKIE", "") and (
            only is None or "handshake" in only
        ):
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
            stats[src] = {"count": 0, "errors": [], "enabled": False}
    except Exception as exc:
        logger.error("Playwright scrapers fatal error: %s", exc)
        for src in ("wellfound", "dice", "handshake"):
            stats.setdefault(src, {"count": 0, "errors": [str(exc)], "enabled": True})

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
    )


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


def fetch_and_save_jobs(db: Session, only: set[str] | None = None) -> dict:
    """
    Run one fetch cycle. `only` restricts it to the named sources, which is what
    makes testing a single adapter take seconds instead of minutes.
    """
    started_at = datetime.now(timezone.utc)
    counts = {"fetched": 0, "inserted": 0, "merged": 0, "skipped": 0, "stale": 0, "sources": {}}

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
        with SourceLogCapture() as capture:
            raw_jobs, source_stats = _run_all_adapters(
                queries, locations, settings, ats_slugs, loc_prefs, only
            )
        merge_into_stats(source_stats, capture.messages)
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
    profile.data = updated_data
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

    max_age_days = getattr(settings, "MAX_JOB_AGE_DAYS", 30)

    # Per-source outcomes. "Fetched" alone flatters a source that returns the
    # same postings every cycle; what matters is how many were actually new.
    per_source: dict[str, dict] = {}

    def _tally(source: str, outcome: str) -> None:
        entry = per_source.setdefault(
            source, {"inserted": 0, "merged": 0, "skipped": 0, "stale": 0}
        )
        entry[outcome] += 1

    for job_data in raw_jobs:
        try:
            url = job_data.get("url", "")
            source = job_data.get("source", "")
            source_job_id = job_data.get("source_job_id")
            company = job_data.get("company", "")
            title = job_data.get("title", "")
            location = job_data.get("location", "")
            description = job_data.get("description", "")
            apply_url = job_data.get("apply_url")

            # Skip stale postings: they're usually filled or unresponsive, and
            # they waste LLM matching calls and applications.
            posted_at = _parse_posted_at(job_data.get("posted_at"))
            if posted_at and max_age_days and (now - posted_at).days > max_age_days:
                counts["stale"] += 1
                _tally(source, "stale")
                continue

            dedupe_hash = compute_dedupe_hash(company, title, location)
            existing = find_existing_job(db, source, url, source_job_id, dedupe_hash)

            if existing is not None:
                # A direct apply link is worth backfilling even on a job we're
                # otherwise skipping — it's what the user actually clicks.
                if apply_url and not existing.apply_url:
                    existing.apply_url = apply_url
                if url in existing.source_urls:
                    counts["skipped"] += 1
                    _tally(source, "skipped")
                    continue
                if source_job_id and existing.source_job_id == source_job_id and existing.source == source:
                    counts["skipped"] += 1
                    _tally(source, "skipped")
                    continue
                merge_or_skip(db, existing, url, description, layer=3)
                counts["merged"] += 1
                _tally(source, "merged")
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
                description=description,
                experience_level=job_data.get("experience_level", "mid"),
                status=JobStatus.new,
                fetched_at=now,
                posted_at=posted_at,
                dedupe_hash=dedupe_hash,
            )
            db.add(new_job)
            db.flush()
            counts["inserted"] += 1
            _tally(source, "inserted")

        except Exception as exc:
            logger.error("job_fetcher: error processing job: %s", exc)

    try:
        db.commit()
    except Exception as exc:
        logger.error("job_fetcher: DB commit failed: %s", exc)
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
        "=== fetch cycle done in %.1fs — fetched=%d new=%d merged=%d dup=%d stale=%d ===",
        elapsed, counts["fetched"], counts["inserted"], counts["merged"],
        counts["skipped"], counts["stale"],
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
