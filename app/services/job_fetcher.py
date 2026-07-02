import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import settings
from app.models.job import Job, JobStatus
from app.models.profile import Profile
from app.services.deduplication import compute_dedupe_hash, find_existing_job, merge_or_skip

logger = logging.getLogger(__name__)


def _get_slugs(raw: str) -> list[str]:
    return [s.strip() for s in raw.split(",") if s.strip()]


def _record(stats: dict, source: str, jobs: list[dict], error: str | None = None) -> None:
    """Accumulate per-source fetch stats."""
    entry = stats.setdefault(source, {"count": 0, "errors": []})
    entry["count"] += len(jobs)
    if error:
        entry["errors"].append(error)


def _run_all_adapters(
    roles: list[str], locations: list[str], cfg, ats_slugs: dict | None = None
) -> tuple[list[dict], dict]:
    """
    Call all enabled adapters and return (all_jobs, source_stats).
    source_stats: {source: {"count": N, "errors": [...], "enabled": bool}}
    ats_slugs: final slug list per ATS (configured + seeds + discovered), built
    by the caller; when None, assembled from settings alone.
    """
    from app.services.ats_discovery import build_ats_slugs

    if ats_slugs is None:
        ats_slugs = build_ats_slugs(cfg)

    all_jobs: list[dict] = []
    stats: dict = {}

    # --- Tier 1: httpx adapters ---

    if cfg.ADZUNA_APP_ID and cfg.ADZUNA_APP_KEY:
        from app.services.sources.adzuna import fetch as adzuna_fetch
        stats.setdefault("adzuna", {"count": 0, "errors": [], "enabled": True})
        for role in roles:
            for loc in locations:
                try:
                    jobs = adzuna_fetch(app_id=cfg.ADZUNA_APP_ID, app_key=cfg.ADZUNA_APP_KEY,
                                       query=role, location=loc)
                    _record(stats, "adzuna", jobs)
                    all_jobs.extend(jobs)
                except Exception as exc:
                    _record(stats, "adzuna", [], f"{role}/{loc}: {exc}")
    else:
        stats["adzuna"] = {"count": 0, "errors": [], "enabled": False}

    if cfg.JSEARCH_API_KEY:
        from app.services.sources.jsearch import fetch as jsearch_fetch
        stats.setdefault("jsearch", {"count": 0, "errors": [], "enabled": True})
        for role in roles:
            for loc in locations:
                try:
                    jobs = jsearch_fetch(api_key=cfg.JSEARCH_API_KEY, query=role, location=loc)
                    _record(stats, "jsearch", jobs)
                    all_jobs.extend(jobs)
                except Exception as exc:
                    _record(stats, "jsearch", [], f"{role}/{loc}: {exc}")
    else:
        stats["jsearch"] = {"count": 0, "errors": [], "enabled": False}

    greenhouse_slugs = ats_slugs.get("greenhouse") or []
    if greenhouse_slugs:
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
    if lever_slugs:
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
    if ashby_slugs:
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
        if slugs:
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
    if workday_tenants:
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
    if cfg.JOOBLE_API_KEY:
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

    # --- Findwork: keyed developer-jobs API (free key) ---
    if cfg.FINDWORK_API_KEY:
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
    from app.services.sources.linkedin import fetch as li_fetch
    stats.setdefault("linkedin", {"count": 0, "errors": [], "enabled": True})
    for role in roles:
        for loc in locations:
            try:
                jobs = li_fetch(session_cookie=cfg.LINKEDIN_SESSION_COOKIE, query=role, location=loc)
                _record(stats, "linkedin", jobs)
                all_jobs.extend(jobs)
            except Exception as exc:
                _record(stats, "linkedin", [], f"{role}/{loc}: {exc}")

    # --- Indeed: httpx RSS feed (no browser needed) ---
    from app.services.sources.indeed import fetch as indeed_fetch
    stats.setdefault("indeed", {"count": 0, "errors": [], "enabled": True})
    for role in roles:
        for loc in locations:
            try:
                jobs = indeed_fetch(query=role, location=loc)
                _record(stats, "indeed", jobs)
                all_jobs.extend(jobs)
            except Exception as exc:
                _record(stats, "indeed", [], f"{role}/{loc}: {exc}")

    # --- Remotive: free public API for remote tech jobs ---
    from app.services.sources.remotive import fetch as remotive_fetch
    stats.setdefault("remotive", {"count": 0, "errors": [], "enabled": True})
    for role in roles:
        try:
            jobs = remotive_fetch(query=role)
            _record(stats, "remotive", jobs)
            all_jobs.extend(jobs)
        except Exception as exc:
            _record(stats, "remotive", [], f"{role}: {exc}")

    # --- Arbeitnow: free public API ---
    from app.services.sources.arbeitnow import fetch as arbeitnow_fetch
    stats.setdefault("arbeitnow", {"count": 0, "errors": [], "enabled": True})
    for role in roles:
        for loc in locations:
            try:
                jobs = arbeitnow_fetch(query=role, location=loc)
                _record(stats, "arbeitnow", jobs)
                all_jobs.extend(jobs)
            except Exception as exc:
                _record(stats, "arbeitnow", [], f"{role}/{loc}: {exc}")

    # --- RemoteOK: free public API for remote tech jobs ---
    from app.services.sources.remoteok import fetch as remoteok_fetch
    stats.setdefault("remoteok", {"count": 0, "errors": [], "enabled": True})
    for role in roles:
        try:
            jobs = remoteok_fetch(query=role)
            _record(stats, "remoteok", jobs)
            all_jobs.extend(jobs)
        except Exception as exc:
            _record(stats, "remoteok", [], f"{role}: {exc}")

    # --- We Work Remotely: RSS feed for remote tech jobs ---
    from app.services.sources.weworkremotely import fetch as wwr_fetch
    stats.setdefault("weworkremotely", {"count": 0, "errors": [], "enabled": True})
    for role in roles:
        try:
            jobs = wwr_fetch(query=role)
            _record(stats, "weworkremotely", jobs)
            all_jobs.extend(jobs)
        except Exception as exc:
            _record(stats, "weworkremotely", [], f"{role}: {exc}")

    # --- The Muse: free public API, tech categories ---
    from app.services.sources.themuse import fetch as themuse_fetch
    stats.setdefault("themuse", {"count": 0, "errors": [], "enabled": True})
    for role in roles:
        try:
            jobs = themuse_fetch(query=role)
            _record(stats, "themuse", jobs)
            all_jobs.extend(jobs)
        except Exception as exc:
            _record(stats, "themuse", [], f"{role}: {exc}")

    # --- Himalayas: free public API for remote tech jobs ---
    from app.services.sources.himalayas import fetch as himalayas_fetch
    stats.setdefault("himalayas", {"count": 0, "errors": [], "enabled": True})
    for role in roles:
        try:
            jobs = himalayas_fetch(query=role)
            _record(stats, "himalayas", jobs)
            all_jobs.extend(jobs)
        except Exception as exc:
            _record(stats, "himalayas", [], f"{role}: {exc}")

    # --- Jobicy: free public API for remote tech jobs ---
    from app.services.sources.jobicy import fetch as jobicy_fetch
    stats.setdefault("jobicy", {"count": 0, "errors": [], "enabled": True})
    for role in roles:
        try:
            jobs = jobicy_fetch(query=role)
            _record(stats, "jobicy", jobs)
            all_jobs.extend(jobs)
        except Exception as exc:
            _record(stats, "jobicy", [], f"{role}: {exc}")

    # --- Hacker News "Who is hiring?": one monthly thread, fetched once ---
    from app.services.sources.hnhiring import fetch as hn_fetch
    stats.setdefault("hnhiring", {"count": 0, "errors": [], "enabled": True})
    try:
        jobs = hn_fetch(queries=roles)
        _record(stats, "hnhiring", jobs)
        all_jobs.extend(jobs)
    except Exception as exc:
        _record(stats, "hnhiring", [], str(exc))

    # --- Tier 2: Playwright scrapers (Wellfound, Dice, Handshake) ---

    async def _run_playwright() -> tuple[list[dict], dict]:
        pw_jobs: list[dict] = []
        pw_stats: dict = {}

        from app.services.sources.wellfound import fetch as wf_fetch
        pw_stats.setdefault("wellfound", {"count": 0, "errors": [], "enabled": True})
        for role in roles:
            for loc in locations:
                try:
                    jobs = await wf_fetch(query=role, location=loc)
                    _record(pw_stats, "wellfound", jobs)
                    pw_jobs.extend(jobs)
                except Exception as exc:
                    _record(pw_stats, "wellfound", [], f"{role}/{loc}: {exc}")

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

        if getattr(cfg, "HANDSHAKE_SESSION_COOKIE", ""):
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
        pw_jobs, pw_stats = asyncio.run(_run_playwright())
        all_jobs.extend(pw_jobs)
        stats.update(pw_stats)
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


def fetch_and_save_jobs(db: Session) -> dict:
    counts = {"fetched": 0, "inserted": 0, "merged": 0, "skipped": 0, "stale": 0, "sources": {}}

    profile = db.query(Profile).first()
    if not profile:
        logger.warning("job_fetcher: no profile found, skipping.")
        return counts

    roles: list[str] = profile.data.get("target_roles") or []
    locations: list[str] = profile.data.get("target_locations") or []

    if not roles or not locations:
        logger.warning("job_fetcher: target_roles or target_locations empty.")
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
    from app.services.ats_discovery import build_ats_slugs, configured_ats_slugs
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
    ats_slugs = build_ats_slugs(settings, discovered_ats, validated_configured)

    try:
        raw_jobs, source_stats = _run_all_adapters(queries, locations, settings, ats_slugs)
    except Exception as exc:
        logger.error("job_fetcher: _run_all_adapters failed: %s", exc)
        return counts

    counts["fetched"] = len(raw_jobs)
    counts["sources"] = source_stats
    now = datetime.now(timezone.utc)

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
    updated_data["last_fetch"] = {
        "at": now.isoformat(),
        "fetched": len(raw_jobs),
        "sources": {
            src: {"count": s["count"], "enabled": s["enabled"],
                  "errors": s["errors"][:3]}  # cap at 3 errors stored
            for src, s in source_stats.items()
        },
    }
    profile.data = updated_data

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

    for job_data in raw_jobs:
        try:
            url = job_data.get("url", "")
            source = job_data.get("source", "")
            source_job_id = job_data.get("source_job_id")
            company = job_data.get("company", "")
            title = job_data.get("title", "")
            location = job_data.get("location", "")
            description = job_data.get("description", "")

            # Skip stale postings: they're usually filled or unresponsive, and
            # they waste LLM matching calls and applications.
            posted_at = _parse_posted_at(job_data.get("posted_at"))
            if posted_at and max_age_days and (now - posted_at).days > max_age_days:
                counts["stale"] += 1
                continue

            dedupe_hash = compute_dedupe_hash(company, title, location)
            existing = find_existing_job(db, source, url, source_job_id, dedupe_hash)

            if existing is not None:
                if url in existing.source_urls:
                    counts["skipped"] += 1
                    continue
                if source_job_id and existing.source_job_id == source_job_id and existing.source == source:
                    counts["skipped"] += 1
                    continue
                merge_or_skip(db, existing, url, description, layer=3)
                counts["merged"] += 1
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

        except Exception as exc:
            logger.error("job_fetcher: error processing job: %s", exc)

    try:
        db.commit()
    except Exception as exc:
        logger.error("job_fetcher: DB commit failed: %s", exc)
        db.rollback()

    logger.info(
        "job_fetcher done — fetched=%d inserted=%d merged=%d skipped=%d stale=%d",
        counts["fetched"], counts["inserted"], counts["merged"], counts["skipped"], counts["stale"],
    )
    return counts
