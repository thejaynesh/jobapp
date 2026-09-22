import logging
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
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
                    "choices": tunables.choices_for(t, data),
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
            {"label": "Working Nomads", "ok": True, "builtin": True},
            {"label": "Built In", "ok": getattr(cfg, "BUILTIN_ENABLED", True), "builtin": True},
            {"label": "Jobspresso", "ok": True, "builtin": True},
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


def _model_card(profile_data: dict, provider: str, **extra) -> dict:
    """One provider's model list, as the page renders it."""
    from app.services import model_catalog

    endpoint = model_catalog._endpoint(provider)
    return {
        "provider": provider,
        "label": model_catalog.PROVIDER_LABELS[provider],
        "models": model_catalog.models(profile_data, provider),
        "custom": model_catalog.saved(profile_data, provider) is not None,
        "configured": bool(endpoint and (endpoint[1] or "").strip()),
        "discovered": None,
        "message": "",
        "ok": True,
        "rejected": [],
        **extra,
    }


def _model_cards(profile_data: dict) -> list[dict]:
    from app.services import model_catalog

    return [_model_card(profile_data, provider) for provider, _ in model_catalog.PROVIDERS]


def _render_card(request: Request, card: dict, first: str | None):
    return templates.TemplateResponse(
        request, "settings/partials/model_list.html",
        {"card": card, "first": bool(first)},
    )


def _known_provider(provider: str) -> None:
    from app.services import model_catalog

    if provider not in model_catalog.PROVIDER_LABELS:
        raise HTTPException(status_code=404, detail="Unknown provider")


@router.post("/models/{provider}", response_class=HTMLResponse)
def save_model_list(request: Request, provider: str, models: str = Form(""),
                    first: str = Form(""), db: Session = Depends(get_db)):
    """
    Replace one provider's model list with what was typed.

    Ids that are not shaped like a model id are reported back rather than
    silently dropped, so a pasted sentence does not vanish unexplained.
    """
    from app.services import model_catalog

    _known_provider(provider)
    valid, rejected = model_catalog.parse(models)
    profile = get_or_create_profile(db)
    if not valid:
        card = _model_card(profile.data, provider, ok=False, rejected=rejected,
                           message="Nothing to save — the list needs at least one model. "
                                   "Use “Reset to defaults” to go back to the built-in list.")
        return _render_card(request, card, first)
    profile.data = model_catalog.store(profile.data, provider, valid)
    db.commit()
    card = _model_card(profile.data, provider, rejected=rejected,
                       message=f"Saved {len(valid)} model{'s' if len(valid) != 1 else ''}. "
                               "They are in the model dropdowns above after a reload.")
    return _render_card(request, card, first)


@router.post("/models/{provider}/reset", response_class=HTMLResponse)
def reset_model_list(request: Request, provider: str, first: str = Form(""),
                     db: Session = Depends(get_db)):
    from app.services import model_catalog

    _known_provider(provider)
    profile = get_or_create_profile(db)
    profile.data = model_catalog.reset(profile.data, provider)
    db.commit()
    return _render_card(request, _model_card(profile.data, provider,
                                             message="Back on the built-in list."), first)


@router.post("/models/{provider}/discover", response_class=HTMLResponse)
def discover_models(request: Request, provider: str, first: str = Form(""),
                    db: Session = Depends(get_db)):
    """Ask the provider what it serves, and offer what is not on the list yet."""
    from app.services import model_catalog

    _known_provider(provider)
    profile = get_or_create_profile(db)
    try:
        found = model_catalog.discover(provider)
    except ValueError as exc:
        return _render_card(request, _model_card(profile.data, provider, ok=False,
                                                 message=str(exc)), first)
    current = set(model_catalog.models(profile.data, provider))
    new = [m for m in found if m not in current]
    return _render_card(request, _model_card(profile.data, provider, discovered=new),
                        first)


@router.post("/models/{provider}/add", response_class=HTMLResponse)
async def add_models(request: Request, provider: str, db: Session = Depends(get_db)):
    """Append the ticked discoveries to the provider's list."""
    from app.services import model_catalog

    _known_provider(provider)
    form = await request.form()
    ticked = [m for m in form.getlist("add") if model_catalog.is_model_id(m)]
    profile = get_or_create_profile(db)
    if not ticked:
        return _render_card(request, _model_card(profile.data, provider, ok=False,
                                                 message="Nothing was ticked."),
                            form.get("first"))
    merged = model_catalog.models(profile.data, provider) + ticked
    profile.data = model_catalog.store(profile.data, provider, merged)
    db.commit()
    card = _model_card(profile.data, provider,
                       message=f"Added {len(ticked)}. They are in the model dropdowns "
                               "above after a reload.")
    return _render_card(request, card, form.get("first"))


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
        "model_cards": _model_cards(profile.data or {}),
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
    profile.data = tunables.apply_to_profile(
        profile.data, tunables.parse_form(form, profile.data)
    )
    db.commit()
    return templates.TemplateResponse(
        "settings/index.html", _page_context(request, profile, db, True)
    )
