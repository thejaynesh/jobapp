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


def _run_all_adapters(roles: list[str], locations: list[str], cfg) -> list[dict]:
    """
    Call all enabled Tier 1 (httpx) and Tier 2 (Playwright) adapters.
    Each adapter wraps errors internally and returns []. Never raises.
    """
    all_jobs: list[dict] = []

    # Tier 1: httpx adapters
    if cfg.ADZUNA_APP_ID and cfg.ADZUNA_APP_KEY:
        from app.services.sources.adzuna import fetch as adzuna_fetch
        for role in roles:
            for loc in locations:
                try:
                    all_jobs.extend(adzuna_fetch(
                        app_id=cfg.ADZUNA_APP_ID, app_key=cfg.ADZUNA_APP_KEY,
                        query=role, location=loc,
                    ))
                except Exception as exc:
                    logger.error("Adzuna error for '%s'/'%s': %s", role, loc, exc)

    if cfg.JSEARCH_API_KEY:
        from app.services.sources.jsearch import fetch as jsearch_fetch
        for role in roles:
            for loc in locations:
                try:
                    all_jobs.extend(jsearch_fetch(
                        api_key=cfg.JSEARCH_API_KEY, query=role, location=loc,
                    ))
                except Exception as exc:
                    logger.error("JSearch error for '%s'/'%s': %s", role, loc, exc)

    greenhouse_slugs = _get_slugs(cfg.GREENHOUSE_COMPANY_SLUGS)
    if greenhouse_slugs:
        from app.services.sources.greenhouse import fetch as gh_fetch
        try:
            all_jobs.extend(gh_fetch(company_slugs=greenhouse_slugs))
        except Exception as exc:
            logger.error("Greenhouse error: %s", exc)

    lever_slugs = _get_slugs(cfg.LEVER_COMPANY_SLUGS)
    if lever_slugs:
        from app.services.sources.lever import fetch as lever_fetch
        try:
            all_jobs.extend(lever_fetch(company_slugs=lever_slugs))
        except Exception as exc:
            logger.error("Lever error: %s", exc)

    ashby_slugs = _get_slugs(cfg.ASHBY_COMPANY_SLUGS)
    if ashby_slugs:
        from app.services.sources.ashby import fetch as ashby_fetch
        try:
            all_jobs.extend(ashby_fetch(company_slugs=ashby_slugs))
        except Exception as exc:
            logger.error("Ashby error: %s", exc)

    # Tier 2: Playwright scrapers (async, run in new event loop)
    async def _run_playwright() -> list[dict]:
        pw_jobs: list[dict] = []

        if cfg.LINKEDIN_SESSION_COOKIE:
            from app.services.sources.linkedin import fetch as li_fetch
            for role in roles:
                for loc in locations:
                    try:
                        pw_jobs.extend(await li_fetch(
                            session_cookie=cfg.LINKEDIN_SESSION_COOKIE,
                            query=role, location=loc,
                        ))
                    except Exception as exc:
                        logger.error("LinkedIn error: %s", exc)

        for role in roles:
            for loc in locations:
                from app.services.sources.indeed import fetch as indeed_fetch
                try:
                    pw_jobs.extend(await indeed_fetch(query=role, location=loc))
                except Exception as exc:
                    logger.error("Indeed error: %s", exc)

                from app.services.sources.wellfound import fetch as wf_fetch
                try:
                    pw_jobs.extend(await wf_fetch(query=role, location=loc))
                except Exception as exc:
                    logger.error("Wellfound error: %s", exc)

                from app.services.sources.dice import fetch as dice_fetch
                try:
                    pw_jobs.extend(await dice_fetch(query=role, location=loc))
                except Exception as exc:
                    logger.error("Dice error: %s", exc)

        if getattr(cfg, "HANDSHAKE_SESSION_COOKIE", ""):
            from app.services.sources.handshake import fetch as hs_fetch
            for role in roles:
                try:
                    pw_jobs.extend(await hs_fetch(
                        session_cookie=cfg.HANDSHAKE_SESSION_COOKIE,
                        query=role, location="",
                    ))
                except Exception as exc:
                    logger.error("Handshake error: %s", exc)

        return pw_jobs

    try:
        pw_results = asyncio.run(_run_playwright())
        all_jobs.extend(pw_results)
    except Exception as exc:
        logger.error("Playwright scrapers fatal error: %s", exc)

    return all_jobs


def fetch_and_save_jobs(db: Session) -> dict:
    counts = {"fetched": 0, "inserted": 0, "merged": 0, "skipped": 0}

    profile = db.query(Profile).first()
    if not profile:
        logger.warning("job_fetcher: no profile found, skipping.")
        return counts

    roles: list[str] = profile.data.get("target_roles") or []
    locations: list[str] = profile.data.get("target_locations") or []

    if not roles or not locations:
        logger.warning("job_fetcher: target_roles or target_locations empty.")
        return counts

    try:
        raw_jobs = _run_all_adapters(roles, locations, settings)
    except Exception as exc:
        logger.error("job_fetcher: _run_all_adapters failed: %s", exc)
        return counts

    counts["fetched"] = len(raw_jobs)
    now = datetime.now(timezone.utc)

    def _parse_posted_at(raw) -> datetime | None:
        if raw is None:
            return None
        if isinstance(raw, (int, float)):
            try:
                return datetime.fromtimestamp(raw, tz=timezone.utc)
            except Exception:
                return None
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except Exception:
            return None

    for job_data in raw_jobs:
        try:
            url = job_data.get("url", "")
            source = job_data.get("source", "")
            source_job_id = job_data.get("source_job_id")
            company = job_data.get("company", "")
            title = job_data.get("title", "")
            location = job_data.get("location", "")
            description = job_data.get("description", "")

            dedupe_hash = compute_dedupe_hash(company, title, location)
            existing = find_existing_job(db, source, url, source_job_id, dedupe_hash)

            if existing is not None:
                if url in existing.source_urls:
                    counts["skipped"] += 1
                    continue
                if source_job_id and existing.source_job_id == source_job_id and existing.source == source:
                    counts["skipped"] += 1
                    continue
                # Hash-only match = cross-post: merge
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
                posted_at=_parse_posted_at(job_data.get("posted_at")),
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

    return counts
