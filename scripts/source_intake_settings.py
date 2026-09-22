"""Read-only source configuration snapshot. Prints no credential values.

Run inside a deployed worker with `python - < scripts/source_intake_settings.py`.
Use worker, not just web: fetches execute with the worker's environment.
"""

import json

from app.config import settings

# Deliberate allowlist: never dump settings, environment, profile or cookies.
PUBLIC_FIELDS = """
FETCH_API_INTERVAL_HOURS FETCH_BOARDS_INTERVAL_HOURS FETCH_BROWSER_INTERVAL_HOURS
FETCH_LINKED_INTERVAL_HOURS FETCH_LINKED_DEEP_INTERVAL_HOURS BROWSER_TIER_ENABLED
HIRINGCAFE_ENABLED YC_ENABLED BUILTIN_ENABLED INDEED_RSS_ENABLED
WELLFOUND_ENABLED DICE_ENABLED WELLFOUND_ROLES YC_ROLES
ATS_AUTO_DISCOVERY ATS_SEED_COMPANIES ATS_SLUG_VALIDATION ATS_LIST_HARVEST
ATS_BOARD_REGISTRY ATS_MAX_SLUGS_PER_ATS ATS_BOARD_FETCH_WORKERS
ATS_BOARD_MAX_EMPTY_CYCLES ATS_BOARD_VALIDATION ATS_BOARD_VALIDATE_PER_CYCLE
SOURCE_REST_AFTER_FAILURES SOURCE_REST_RETRY_EVERY MAX_JOB_AGE_DAYS
ADZUNA_MAX_PAGES ADZUNA_MAX_DAYS_OLD LINKEDIN_MAX_PAGES LINKEDIN_RECENCY_HOURS
LINKEDIN_MAX_DETAIL_FETCHES USAJOBS_MAX_PAGES ARBEITNOW_MAX_PAGES
BROWSE_ENABLED BROWSE_PAUSED_HOSTS BROWSE_MAX_QUEUED BROWSE_SEARCH_PAGES
BROWSE_SEARCH_RETRY_HOURS BROWSE_SEARCH_RESERVE BROWSE_SCROLL_PASSES
BROWSE_CHALLENGE_BACKOFF_HOURS BROWSE_CHALLENGE_MAX_BACKOFF_HOURS
BROWSE_TOPUP_INTERVAL_MINUTES BROWSE_TOPUP_BELOW BROWSE_AGENT_STALE_HOURS
RESOLVE_APPLY_LINKS LINK_RESOLVE_MAX_PER_CYCLE ATS_SNIFF_CAREER_SITES
ATS_SNIFF_MAX_HOSTS_PER_CYCLE
""".split()

CREDENTIAL_FIELDS = """
ADZUNA_APP_ID ADZUNA_APP_KEY JSEARCH_API_KEY JOOBLE_API_KEY CAREERJET_AFFID
FINDWORK_API_KEY USAJOBS_API_KEY USAJOBS_USER_AGENT HANDSHAKE_SESSION_COOKIE AGENT_TOKEN
""".split()

SLUG_FIELDS = """
GREENHOUSE_COMPANY_SLUGS LEVER_COMPANY_SLUGS ASHBY_COMPANY_SLUGS
SMARTRECRUITERS_COMPANY_SLUGS WORKABLE_COMPANY_SLUGS RECRUITEE_COMPANY_SLUGS
WORKDAY_TENANTS ICIMS_COMPANY_SLUGS BAMBOOHR_COMPANY_SLUGS
TEAMTAILOR_COMPANY_SLUGS JOBVITE_COMPANY_SLUGS PERSONIO_COMPANY_SLUGS
""".split()

result = {
    "worker_environment": {name: getattr(settings, name, None) for name in PUBLIC_FIELDS},
    "credentials_present_only": {
        name: bool(getattr(settings, name, "")) for name in CREDENTIAL_FIELDS
    },
    "configured_board_counts": {
        name: len([slug for slug in getattr(settings, name, "").split(",") if slug.strip()])
        for name in SLUG_FIELDS
    },
}

# One read-only transaction. Only intake-specific effective overrides are shown.
from app.database import SessionLocal
from app.models.profile import Profile
from app.services.tunables import effective_settings
from sqlalchemy import text

with SessionLocal() as db:
    db.execute(text("SET TRANSACTION READ ONLY"))
    db.execute(text("SET LOCAL statement_timeout = '15s'"))
    profile = db.query(Profile).first()
    result["profile_present"] = profile is not None
    if profile is not None:
        cfg = effective_settings(profile.data or {})
        result["effective_intake_settings"] = {
            name: getattr(cfg, name, None)
            for name in ("LINKEDIN_MAX_PAGES", "LINKEDIN_RECENCY_HOURS", "MAX_JOB_AGE_DAYS")
        }
        result["target_role_count"] = len((profile.data or {}).get("target_roles") or [])

print(json.dumps(result, indent=2, sort_keys=True))
