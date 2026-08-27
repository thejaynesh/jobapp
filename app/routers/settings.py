import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from app.templating import build as build_templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.profile_service import get_or_create_profile

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])
templates = build_templates()


def _settings_context(profile) -> dict:
    """
    Current value and env default for every tunable, grouped for the page.

    Declared once in `services.tunables` and rendered from that declaration —
    the previous hand-written trio of fields was written to a key nothing read,
    so all three did nothing at all.
    """
    from app.services import model_roles, tunables

    data = profile.data if profile else {}
    return {
        # What each LLM role would actually use right now. "Auto" on its own
        # answers nothing — the question the page is being asked is "which
        # model is writing my covering letters", and until this existed there
        # was no answer anywhere in the application.
        "model_roles": model_roles.describe(data),
        "tunable_groups": [
            (group, [
                {
                    "spec": t,
                    "value": tunables.value(data, t.key),
                    "default": tunables.default(t),
                    "overridden": tunables.is_overridden(data, t.key),
                    # Rebuilt per render for a dynamic tunable. A provider
                    # whose key was added since the process started would
                    # otherwise never appear in its own dropdown.
                    "choices": (
                        model_roles.choices(t.key.removeprefix("model_"))
                        if t.dynamic else t.choices
                    ),
                }
                for t in tunables.TUNABLES if t.group == group
            ])
            for group in tunables.GROUPS
        ],
    }


def _integrations_status() -> dict:
    """Which external services are configured, grouped by purpose."""
    from app.config import settings as cfg

    def _has(val) -> bool:
        if isinstance(val, str):
            return bool(val.strip())
        return val is not None

    return {
        "llm": [
            {"label": "NVIDIA NIM", "ok": _has(cfg.NVIDIA_NIM_API_KEY),
             "detail": cfg.NVIDIA_NIM_MODEL if _has(cfg.NVIDIA_NIM_API_KEY) else None},
            {"label": "FreeInference", "ok": _has(cfg.FREEINFERENCE_API_KEY),
             "detail": cfg.FREEINFERENCE_MODEL if _has(cfg.FREEINFERENCE_API_KEY) else None},
            {"label": "Anthropic", "ok": _has(cfg.ANTHROPIC_API_KEY),
             "detail": cfg.ANTHROPIC_MODEL if _has(cfg.ANTHROPIC_API_KEY) else None},
            {"label": "Gemini", "ok": _has(cfg.GEMINI_API_KEY),
             "detail": cfg.GEMINI_MODEL if _has(cfg.GEMINI_API_KEY) else None},
        ],
        "sources": [
            {"label": "LinkedIn", "ok": _has(cfg.LINKEDIN_SESSION_COOKIE)},
            {"label": "Adzuna", "ok": _has(cfg.ADZUNA_APP_ID) and _has(cfg.ADZUNA_APP_KEY)},
            {"label": "JSearch", "ok": _has(cfg.JSEARCH_API_KEY)},
            {"label": "Jooble", "ok": _has(cfg.JOOBLE_API_KEY)},
            {"label": "FindWork", "ok": _has(cfg.FINDWORK_API_KEY)},
            {"label": "CareerJet", "ok": _has(cfg.CAREERJET_AFFID)},
            {"label": "USAJobs", "ok": _has(cfg.USAJOBS_API_KEY)},
            {"label": "Handshake", "ok": _has(cfg.HANDSHAKE_SESSION_COOKIE)},
            {"label": "HiringCafe", "ok": cfg.HIRINGCAFE_ENABLED, "builtin": True},
            {"label": "Y Combinator", "ok": cfg.YC_ENABLED, "builtin": True},
            {"label": "Dice", "ok": cfg.DICE_ENABLED, "builtin": True},
            {"label": "Arbeitnow", "ok": True, "builtin": True},
            {"label": "Indeed RSS", "ok": cfg.INDEED_RSS_ENABLED, "builtin": True},
            {"label": "Wellfound", "ok": cfg.WELLFOUND_ENABLED, "builtin": True},
        ],
        "outreach": [
            {"label": "Hunter.io", "ok": _has(cfg.HUNTER_IO_API_KEY)},
            {"label": "GitHub", "ok": _has(cfg.GITHUB_TOKEN) and cfg.OUTREACH_USE_GITHUB},
            {"label": "SMTP (send)", "ok": _has(cfg.SMTP_HOST) and cfg.OUTREACH_SEND_ENABLED},
            {"label": "IMAP (read)", "ok": _has(cfg.IMAP_HOST) and cfg.IMAP_ENABLED},
        ],
    }


def _feature_flags() -> list[tuple]:
    """Boolean feature flags from config, grouped by category."""
    from app.config import settings as cfg

    return [
        ("Pipeline", [
            ("Enrichment", cfg.ENRICH_ENABLED, "ENRICH_ENABLED"),
            ("Deep matching", cfg.DEEP_MATCH_ENABLED, "DEEP_MATCH_ENABLED"),
            ("Doc refresh", cfg.DOC_REFRESH_ENABLED, "DOC_REFRESH_ENABLED"),
            ("Self-review", cfg.SELF_REVIEW_ENABLED, "SELF_REVIEW_ENABLED"),
            ("Liveness checks", cfg.LIVENESS_ENABLED, "LIVENESS_ENABLED"),
            ("Archiving", cfg.ARCHIVE_ENABLED, "ARCHIVE_ENABLED"),
            ("LLM call log", cfg.LLM_LOG_ENABLED, "LLM_LOG_ENABLED"),
        ]),
        ("Browsing", [
            ("Driven browsing", cfg.BROWSE_ENABLED, "BROWSE_ENABLED"),
            ("Browser tier", cfg.BROWSER_TIER_ENABLED, "BROWSER_TIER_ENABLED"),
        ]),
        ("Discovery", [
            ("Auto-discovery", cfg.ATS_AUTO_DISCOVERY, "ATS_AUTO_DISCOVERY"),
            ("Seed companies", cfg.ATS_SEED_COMPANIES, "ATS_SEED_COMPANIES"),
            ("Slug validation", cfg.ATS_SLUG_VALIDATION, "ATS_SLUG_VALIDATION"),
            ("List harvest", cfg.ATS_LIST_HARVEST, "ATS_LIST_HARVEST"),
            ("Board registry", cfg.ATS_BOARD_REGISTRY, "ATS_BOARD_REGISTRY"),
            ("Board validation", cfg.ATS_BOARD_VALIDATION, "ATS_BOARD_VALIDATION"),
            ("Career site sniff", cfg.ATS_SNIFF_CAREER_SITES, "ATS_SNIFF_CAREER_SITES"),
            ("Link resolution", cfg.RESOLVE_APPLY_LINKS, "RESOLVE_APPLY_LINKS"),
        ]),
        ("Outreach", [
            ("Outreach enabled", cfg.OUTREACH_ENABLED, "OUTREACH_ENABLED"),
            ("Send emails", cfg.OUTREACH_SEND_ENABLED, "OUTREACH_SEND_ENABLED"),
            ("LinkedIn people", cfg.OUTREACH_USE_LINKEDIN, "OUTREACH_USE_LINKEDIN"),
            ("GitHub people", cfg.OUTREACH_USE_GITHUB, "OUTREACH_USE_GITHUB"),
            ("Team pages", cfg.OUTREACH_USE_TEAM_PAGES, "OUTREACH_USE_TEAM_PAGES"),
            ("Guess emails", cfg.OUTREACH_GUESS_EMAILS, "OUTREACH_GUESS_EMAILS"),
            ("Verify emails", cfg.OUTREACH_VERIFY_EMAILS, "OUTREACH_VERIFY_EMAILS"),
            ("Auto follow-ups", cfg.OUTREACH_AUTO_DRAFT_FOLLOWUPS, "OUTREACH_AUTO_DRAFT_FOLLOWUPS"),
            ("IMAP polling", cfg.IMAP_ENABLED, "IMAP_ENABLED"),
        ]),
        ("Maintenance", [
            ("Backups", cfg.BACKUP_ENABLED, "BACKUP_ENABLED"),
            ("Harvest samples", cfg.HARVEST_SAMPLES_ENABLED, "HARVEST_SAMPLES_ENABLED"),
            ("Board backfill", cfg.BOARD_BACKFILL_ON_START, "BOARD_BACKFILL_ON_START"),
        ]),
    ]


def _system_info() -> dict:
    """Key system parameters the user should see at a glance."""
    from app.config import settings as cfg

    return {
        "timezone": cfg.DISPLAY_TIMEZONE,
        "match_primary": cfg.MATCH_PRIMARY,
        "fetch_api_hours": cfg.FETCH_API_INTERVAL_HOURS,
        "fetch_boards_hours": cfg.FETCH_BOARDS_INTERVAL_HOURS,
        "fetch_browser_hours": cfg.FETCH_BROWSER_INTERVAL_HOURS,
        "match_interval_min": cfg.MATCH_INTERVAL_MINUTES,
        "enrich_interval_min": cfg.ENRICH_INTERVAL_MINUTES,
        "deep_band": f"{cfg.DEEP_MATCH_BAND_LOW}–{cfg.DEEP_MATCH_BAND_HIGH}",
        "max_paid_calls": cfg.MAX_PAID_MATCH_CALLS_PER_CYCLE,
        "auth_enabled": cfg.AUTH_ENABLED,
        "debug": cfg.DEBUG,
    }


def _board_registry(db: Session) -> dict:
    """Per-ATS board counts; never let a registry hiccup break the page."""
    try:
        from app.services.company_boards import summary
        return summary(db)
    except Exception as exc:
        logger.warning("settings: board registry summary failed: %s", exc)
        return {}


def _retired_boards(db: Session) -> list:
    """Boards that stopped returning jobs and are no longer polled."""
    try:
        from app.services.company_boards import retired_boards
        # Materialise here: a lazy/failed result blowing up mid-render would
        # take the whole settings page down.
        return list(retired_boards(db))
    except Exception as exc:
        logger.warning("settings: retired board lookup failed: %s", exc)
        return []


def _page_context(request: Request, profile, db: Session, saved: bool) -> dict:
    integrations = _integrations_status()
    flags = _feature_flags()
    settings_ctx = _settings_context(profile)

    enabled_count = sum(
        sum(1 for _, val, _ in items if val) for _, items in flags
    )
    total_flags = sum(len(items) for _, items in flags)

    return {
        "request": request,
        "saved": saved,
        **settings_ctx,
        "last_fetch": profile.data.get("last_fetch"),
        "board_registry": _board_registry(db),
        "retired_boards": _retired_boards(db),
        "slug_report": profile.data.get("ats_slug_report") or {},
        "integrations": integrations,
        "feature_flags": flags,
        "system_info": _system_info(),
        "summary": {
            "llm_count": sum(1 for i in integrations["llm"] if i["ok"]),
            "source_count": sum(1 for i in integrations["sources"] if i["ok"]),
            "outreach_count": sum(1 for i in integrations["outreach"] if i["ok"]),
            "flags_enabled": enabled_count,
            "flags_total": total_flags,
            "tunable_count": sum(len(items) for _, items in settings_ctx["tunable_groups"]),
        },
    }


@router.get("", response_class=HTMLResponse)
def get_settings(request: Request, db: Session = Depends(get_db)):
    profile = get_or_create_profile(db)
    db.commit()
    return templates.TemplateResponse(
        "settings/index.html", _page_context(request, profile, db, False)
    )


@router.post("/boards/{board_id}/reactivate", response_class=HTMLResponse)
def reactivate_board(board_id: uuid.UUID, db: Session = Depends(get_db)):
    """Put a retired board back into rotation, e.g. after fixing its slug."""
    from app.services.company_boards import reactivate

    board = reactivate(db, board_id)
    if board is None:
        raise HTTPException(status_code=404, detail="Board not found")
    db.commit()
    # The row removes itself from the "not working" list.
    return HTMLResponse("")


@router.post("", response_class=HTMLResponse)
async def save_settings(request: Request, db: Session = Depends(get_db)):
    """
    Save whatever tunables the form submitted.

    Read from the raw form rather than declared as parameters: the fields come
    from the `TUNABLES` declaration, and duplicating them here is exactly how
    the old version ended up saving three values nobody read.
    """
    from app.services import tunables

    form = dict(await request.form())
    profile = get_or_create_profile(db)
    profile.data = tunables.apply_to_profile(profile.data, tunables.parse_form(form))
    db.commit()
    return templates.TemplateResponse(
        "settings/index.html", _page_context(request, profile, db, True)
    )
