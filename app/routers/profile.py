import copy
import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse
from app.templating import build as build_templates
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.database import get_db
from app.services import bullet_facts
from app.services.locations import REGION_OPTIONS, normalize_prefs
from app.services.profile_service import get_or_create_profile
from app.config import live

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/profile", tags=["profile"])
templates = build_templates()
templates.env.globals["region_options"] = REGION_OPTIONS
templates.env.globals["location_prefs"] = normalize_prefs
# Each entry's bullets with no figure, and the question to ask for one.
templates.env.globals["unanswered_bullets"] = bullet_facts.unanswered


def _skill_alias_lines(profile_data) -> str:
    return "\n".join(" = ".join(group) for group in (profile_data or {}).get("skill_aliases") or []
                     if isinstance(group, list))


templates.env.globals["skill_alias_lines"] = _skill_alias_lines

TABS = ["personal", "experience", "projects", "skills", "education", "stories",
        "screening", "templates", "narrative", "ai prompt", "check", "import"]


@router.get("", response_class=HTMLResponse)
def get_profile(request: Request, tab: str = "personal", db: Session = Depends(get_db)):
    if tab not in TABS:
        tab = "personal"
    profile = get_or_create_profile(db)
    db.commit()
    context = {"request": request, "profile": profile.data, "active_tab": tab}
    if tab == "screening":
        from app.services import screening

        context.update(_screening_context(profile.data))
    if tab == "ai prompt":
        context["preview"] = _preview(db)
    if tab == "check":
        context["check"] = _check(profile.data)
    if tab == "import":
        from app.services import profile_import

        draft = (profile.data or {}).get(profile_import.DRAFT_KEY)
        context["draft"] = draft
        context["review"] = (profile_import.review(draft["parsed"], profile.data)
                             if draft else None)
        context["import_error"] = request.query_params.get("error", "")
        context["import_done"] = request.query_params.get("added", "")
    return templates.TemplateResponse("profile/index.html", context)


_BANK_ACTIONS = ("keep", "dismiss", "use", "remove")


def _bank_changed(request: Request, section: str, item_id: str, change) -> HTMLResponse:
    """Apply a bullet_bank change and re-render the section, the entry's bank open."""
    from app.services import bullet_bank

    if section not in bullet_bank.SECTIONS:
        raise HTTPException(status_code=404, detail="No such section")
    try:
        profile = change()
    except KeyError:
        raise HTTPException(status_code=404, detail="No such entry")
    return templates.TemplateResponse(
        _SECTION_PARTIALS[section],
        {"request": request, "profile": profile.data, "opened": item_id, "panel": "bank"},
    )


@router.post("/{section}/{item_id}/bank", response_class=HTMLResponse)
def add_to_bank(request: Request, section: str, item_id: str, text: str = Form(""),
                db: Session = Depends(get_db)):
    """Keep a wording of your own for this entry."""
    from app.services import bullet_bank

    response = _bank_changed(request, section, item_id,
                             lambda: bullet_bank.add_own(db, section, item_id, text))
    db.commit()
    return response


@router.post("/{section}/{item_id}/bank/{bank_id}/{action}", response_class=HTMLResponse)
def bank_action(request: Request, section: str, item_id: str, bank_id: str, action: str,
                db: Session = Depends(get_db)):
    """Keep, dismiss, use or remove one wording in the entry's bank."""
    from app.services import bullet_bank

    if action not in _BANK_ACTIONS:
        raise HTTPException(status_code=404, detail="No such action")
    response = _bank_changed(request, section, item_id,
                             lambda: getattr(bullet_bank, action)(db, section, item_id, bank_id))
    db.commit()
    return response


def _stories_list(request: Request, profile, saved_id: str | None = None) -> HTMLResponse:
    return templates.TemplateResponse(
        "profile/partials/stories.html",
        {"request": request, "profile": profile.data, "saved_id": saved_id},
    )


def _ensure_stories(db: Session):
    profile = get_or_create_profile(db)
    if not isinstance((profile.data or {}).get("stories"), list):
        data = copy.deepcopy(profile.data or {})
        data["stories"] = []
        profile.data = data
        db.flush()
    return profile


@router.post("/stories/add", response_class=HTMLResponse)
def add_story(request: Request, db: Session = Depends(get_db)):
    from app.services.profile_service import add_list_item
    from app.services.stories import PARTS

    _ensure_stories(db)
    profile = add_list_item(db, "stories", {"title": "", **{p: "" for p in PARTS},
                                            "skills": [], "entry_id": None})
    db.commit()
    return _stories_list(request, profile)


@router.post("/stories/{story_id}", response_class=HTMLResponse)
async def save_story(story_id: str, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    return await run_in_threadpool(_save_story, story_id, request, db, form)


def _save_story(story_id, request, db, form):
    from app.services.profile_service import update_list_item
    from app.services.stories import from_form

    _ensure_stories(db)
    profile = update_list_item(db, "stories", story_id, from_form(form))
    db.commit()
    return _stories_list(request, profile, story_id)


@router.post("/stories/{story_id}/delete", response_class=HTMLResponse)
def delete_story(story_id: str, request: Request, db: Session = Depends(get_db)):
    from app.services.profile_service import remove_list_item

    _ensure_stories(db)
    profile = remove_list_item(db, "stories", story_id)
    db.commit()
    return _stories_list(request, profile)


@router.post("/import")
async def import_profile(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Read an uploaded resume, LinkedIn export or JSON Resume into a draft to review."""
    data = await file.read()
    return await run_in_threadpool(_import_profile, file.filename or "", data, db)


def _import_profile(filename, data, db):
    from urllib.parse import quote

    from app.services import profile_import

    if len(data) > 15 * 1024 * 1024:
        return RedirectResponse(url="/profile?tab=import&error=" + quote("That file is over 15 MB"),
                                status_code=303)
    profile = get_or_create_profile(db)
    profile_data = copy.deepcopy(profile.data or {})
    db.commit()  # release the connection before parsing can call a model
    try:
        source, parsed = profile_import.parse_upload(filename, data, profile_data)
    except profile_import.ImportError_ as exc:
        return RedirectResponse(url="/profile?tab=import&error=" + quote(str(exc)),
                                status_code=303)
    except Exception as exc:
        logger.warning("profile import failed: %s", exc)
        return RedirectResponse(url="/profile?tab=import&error=" + quote(
            f"Could not read it: {str(exc)[:200]}"), status_code=303)
    profile_import.stage(db, source, parsed)
    return RedirectResponse(url="/profile?tab=import", status_code=303)


@router.post("/import/apply")
async def apply_import(request: Request, db: Session = Depends(get_db)):
    """Merge what the review ticked."""
    form = await request.form()
    return await run_in_threadpool(_apply_import, db, form)


def _apply_import(db, form):
    from urllib.parse import quote

    from app.services import profile_import

    try:
        added = profile_import.apply(db, form)
    except profile_import.ImportError_ as exc:
        return RedirectResponse(url="/profile?tab=import&error=" + quote(str(exc)),
                                status_code=303)
    summary = ", ".join(f"{n} {what}" for what, n in added.items() if n) or "nothing"
    return RedirectResponse(url="/profile?tab=import&added=" + quote(summary), status_code=303)


@router.post("/import/discard")
def discard_import(db: Session = Depends(get_db)):
    from app.services import profile_import

    profile_import.discard(db)
    return RedirectResponse(url="/profile?tab=import", status_code=303)


@router.get("/export.json")
def export_json_resume(db: Session = Depends(get_db)):
    """The profile as a JSON Resume file."""
    from fastapi.responses import JSONResponse

    from app.services import profile_import

    profile = get_or_create_profile(db)
    return JSONResponse(profile_import.to_json_resume(profile.data or {}),
                        headers={"Content-Disposition": 'attachment; filename="resume.json"'})


def _check(profile_data: dict) -> dict | None:
    """
    What generation would get from this profile — no LLM, no network.

    Wrapped like the prompt preview: a diagnostic that takes the page down when
    the thing it diagnoses is broken is a diagnostic you cannot use.
    """
    from app.services.profile_check import report

    try:
        return report(profile_data)
    except Exception as exc:
        logger.warning("profile: readiness check unavailable: %s", exc)
        return None


def _preview(db: Session, job_id: str | None = None) -> dict | None:
    from app.services.prompt_preview import build
    try:
        return build(db, job_id)
    except Exception as exc:
        logger.warning("profile: prompt preview unavailable: %s", exc)
        return None


@router.get("/prompt-preview", response_class=HTMLResponse)
def prompt_preview(request: Request, job_id: str | None = None,
                   db: Session = Depends(get_db)):
    """Re-render the preview, optionally against a specific job."""
    profile = get_or_create_profile(db)
    return templates.TemplateResponse(
        "profile/partials/prompt_preview.html",
        {"request": request, "profile": profile.data,
         "preview": _preview(db, job_id)},
    )


@router.post("/personal", response_class=HTMLResponse)
def save_personal(
    request: Request,
    name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    linkedin: str = Form(""),
    github: str = Form(""),
    website: str = Form(""),
    location: str = Form(""),
    db: Session = Depends(get_db),
):
    from app.services.profile_service import save_section
    profile = save_section(db, "personal", {
        "name": name, "email": email, "phone": phone,
        "linkedin": linkedin, "github": github, "website": website, "location": location,
    })
    db.commit()
    return templates.TemplateResponse(
        "profile/partials/personal.html",
        {"request": request, "profile": profile.data, "saved": True},
    )


@router.post("/screening", response_class=HTMLResponse)
async def save_screening(request: Request, db: Session = Depends(get_db)):
    """
    The answer bank the extension types into application forms.

    Read straight off the form rather than through named `Form(...)` arguments,
    because the field list lives in `services.screening` and is shared with the
    autofill projection — a second copy of it in this signature is a second
    place to forget to update.
    """
    form = await request.form()
    return await run_in_threadpool(_save_screening, request, db, dict(form))


def _save_screening(request, db, form):
    from app.services import screening
    from app.services.profile_service import save_section

    profile = save_section(
        db, "screening_answers", screening.clean(dict(form))
    )
    db.commit()
    return templates.TemplateResponse(
        "profile/partials/screening.html",
        {"request": request, "profile": profile.data, "saved": True,
         **_screening_context(profile.data)},
    )


def _screening_context(profile_data: dict) -> dict:
    from app.services import remembered_answers, screening

    remembered = sorted(
        ((key, entry) for key, entry in remembered_answers.entries(profile_data).items()
         if isinstance(entry, dict)),
        key=lambda item: item[1].get("question", "").lower(),
    )
    return {
        "screening_fields": screening.FIELDS,
        "screening": screening.answers(profile_data),
        "remembered": remembered,
    }


@router.post("/remembered/forget", response_class=HTMLResponse)
async def forget_remembered(request: Request, db: Session = Depends(get_db)):
    """Drop one remembered answer; the next form asking it is left for the user."""
    form = await request.form()
    return await run_in_threadpool(_forget_remembered, request, db, str(form.get("key") or ""))


def _forget_remembered(request, db, key):
    from app.services import remembered_answers

    profile = get_or_create_profile(db)
    profile.data = remembered_answers.forget(profile.data or {}, key)
    db.commit()
    return templates.TemplateResponse(
        "profile/partials/screening.html",
        {"request": request, "profile": profile.data, **_screening_context(profile.data)},
    )


@router.post("/experience/add", response_class=HTMLResponse)
def add_experience(request: Request, db: Session = Depends(get_db)):
    from app.services.profile_service import add_list_item
    profile = add_list_item(db, "experience", {
        "company": "", "role": "", "start_date": "", "end_date": "",
        "bullets": [], "tech": [],
    })
    db.commit()
    return templates.TemplateResponse(
        "profile/partials/experience.html",
        {"request": request, "profile": profile.data},
    )


@router.post("/experience/{item_id}", response_class=HTMLResponse)
def save_experience_item(
    request: Request, item_id: str,
    company: str = Form(""), role: str = Form(""),
    start_date: str = Form(""), end_date: str = Form(""),
    bullets: str = Form(""), tech: str = Form(""),
    db: Session = Depends(get_db),
):
    from app.services.profile_service import update_list_item
    profile = update_list_item(db, "experience", item_id, {
        "company": company, "role": role,
        "start_date": start_date, "end_date": end_date,
        "bullets": [b.strip() for b in bullets.splitlines() if b.strip()],
        "tech": [t.strip() for t in tech.split(",") if t.strip()],
    })
    db.commit()
    return templates.TemplateResponse(
        "profile/partials/experience.html",
        {"request": request, "profile": profile.data, "saved_id": item_id},
    )


@router.delete("/experience/{item_id}", response_class=HTMLResponse)
def delete_experience_item(request: Request, item_id: str, db: Session = Depends(get_db)):
    from app.services.profile_service import remove_list_item
    profile = remove_list_item(db, "experience", item_id)
    db.commit()
    return templates.TemplateResponse(
        "profile/partials/experience.html",
        {"request": request, "profile": profile.data},
    )


# Projects
@router.post("/projects/add", response_class=HTMLResponse)
def add_project(request: Request, db: Session = Depends(get_db)):
    from app.services.profile_service import add_list_item
    profile = add_list_item(db, "projects", {"name": "", "description": "", "tech": [], "bullets": [], "url": ""})
    db.commit()
    return templates.TemplateResponse("profile/partials/projects.html", {"request": request, "profile": profile.data})


@router.post("/projects/{item_id}", response_class=HTMLResponse)
def save_project_item(
    request: Request, item_id: str,
    name: str = Form(""), description: str = Form(""),
    tech: str = Form(""), bullets: str = Form(""), url: str = Form(""),
    db: Session = Depends(get_db),
):
    from app.services.profile_service import update_list_item
    profile = update_list_item(db, "projects", item_id, {
        "name": name, "description": description, "url": url,
        "tech": [t.strip() for t in tech.split(",") if t.strip()],
        "bullets": [b.strip() for b in bullets.splitlines() if b.strip()],
    })
    db.commit()
    return templates.TemplateResponse("profile/partials/projects.html", {"request": request, "profile": profile.data, "saved_id": item_id})


@router.delete("/projects/{item_id}", response_class=HTMLResponse)
def delete_project_item(request: Request, item_id: str, db: Session = Depends(get_db)):
    from app.services.profile_service import remove_list_item
    profile = remove_list_item(db, "projects", item_id)
    db.commit()
    return templates.TemplateResponse("profile/partials/projects.html", {"request": request, "profile": profile.data})


# Skills
@router.post("/roles/suggest", response_class=HTMLResponse)
def suggest_roles(request: Request, db: Session = Depends(get_db)):
    """
    Propose target roles the profile supports but the list does not name.

    `target_roles` is the narrowest gate in the pipeline and the one nobody
    revisits: it is typed once during setup and then quietly decides what the
    whole system is allowed to see. A skill picked up since never becomes a
    role, so the postings naming it are rejected on the title before anything
    reads them.

    Suggestions only. Accepting one is a separate click, because widening this
    list changes the meaning of every number on every other page.
    """
    from app.config import settings
    from app.services import role_suggest
    from app.services.tunables import value as tunable

    profile = get_or_create_profile(db)
    profile_data = profile.data or {}

    outcome = role_suggest.suggest(
        profile_data,
        settings.NVIDIA_NIM_API_KEY, settings.NVIDIA_NIM_BASE_URL,
        tunable(profile_data, "nvidia_nim_model"),
    )
    return templates.TemplateResponse(
        "profile/partials/role_suggestions.html",
        {"request": request, "profile": profile_data, **outcome},
    )


@router.post("/roles/add", response_class=HTMLResponse)
def add_target_role(request: Request, title: str = Form(...),
                    db: Session = Depends(get_db)):
    """Accept one suggested role. Returns the skills form, so it shows up."""
    from app.services.profile_service import save_section

    profile = get_or_create_profile(db)
    save_section(db, "target_roles",
                 role_suggest_add(profile.data or {}, title))
    db.commit()

    profile = get_or_create_profile(db)
    return templates.TemplateResponse(
        "profile/partials/skills.html",
        {"request": request, "profile": profile.data or {}, "saved": True},
    )


def role_suggest_add(profile_data: dict, title: str) -> list[str]:
    from app.services.role_suggest import add_role

    return add_role(profile_data, title)


@router.post("/skills", response_class=HTMLResponse)
def save_skills(
    request: Request,
    languages: str = Form(""), frameworks: str = Form(""),
    tools: str = Form(""), clouds: str = Form(""),
    target_roles: str = Form(""),
    location_regions: list[str] = Form(default=[]),
    remote_ok: str = Form(""), custom_locations: str = Form(""),
    excluded_companies: str = Form(""), min_match_score: int = Form(70),
    skill_aliases: str | None = Form(None),
    db: Session = Depends(get_db),
):
    from app.services.locations import REGIONS, search_locations
    from app.services.matcher import parse_alias_lines
    from app.services.profile_service import save_section
    save_section(db, "skills", {
        "languages": [x.strip() for x in languages.split(",") if x.strip()],
        "frameworks": [x.strip() for x in frameworks.split(",") if x.strip()],
        "tools": [x.strip() for x in tools.split(",") if x.strip()],
        "clouds": [x.strip() for x in clouds.split(",") if x.strip()],
    })
    # Absent (an older form) leaves the list alone; present and empty clears it.
    if skill_aliases is not None:
        save_section(db, "skill_aliases", parse_alias_lines(skill_aliases))
    save_section(db, "target_roles", [x.strip() for x in target_roles.splitlines() if x.strip()])
    prefs = {
        "regions": [r for r in location_regions if r in REGIONS],
        "remote_ok": bool(remote_ok),
        "custom": [x.strip() for x in custom_locations.split(",") if x.strip()],
    }
    save_section(db, "location_preferences", prefs)
    # keep the legacy field in sync (derived search strings) for older code/UI
    save_section(db, "target_locations", search_locations(prefs))
    save_section(db, "excluded_companies", [x.strip() for x in excluded_companies.splitlines() if x.strip()])
    # Written through the tunables helper so this and the settings page can't
    # drift apart — the same number lived in two keys, and only one was read.
    from app.services import tunables
    profile = get_or_create_profile(db)
    profile.data = tunables.apply_to_profile(
        profile.data, {"min_match_score": min_match_score}
    )
    db.commit()
    return templates.TemplateResponse("profile/partials/skills.html", {"request": request, "profile": profile.data, "saved": True})


# Education
@router.post("/education/add", response_class=HTMLResponse)
def add_education(request: Request, db: Session = Depends(get_db)):
    from app.services.profile_service import add_list_item
    profile = add_list_item(db, "education", {"school": "", "degree": "", "start_date": "", "end_date": "", "gpa": ""})
    db.commit()
    return templates.TemplateResponse("profile/partials/education.html", {"request": request, "profile": profile.data})


@router.post("/education/{item_id}", response_class=HTMLResponse)
def save_education_item(
    request: Request, item_id: str,
    school: str = Form(""), degree: str = Form(""),
    start_date: str = Form(""), end_date: str = Form(""), gpa: str = Form(""),
    db: Session = Depends(get_db),
):
    from app.services.profile_service import update_list_item
    profile = update_list_item(db, "education", item_id, {"school": school, "degree": degree, "start_date": start_date, "end_date": end_date, "gpa": gpa})
    db.commit()
    return templates.TemplateResponse("profile/partials/education.html", {"request": request, "profile": profile.data, "saved_id": item_id})


@router.delete("/education/{item_id}", response_class=HTMLResponse)
def delete_education_item(request: Request, item_id: str, db: Session = Depends(get_db)):
    from app.services.profile_service import remove_list_item
    profile = remove_list_item(db, "education", item_id)
    db.commit()
    return templates.TemplateResponse("profile/partials/education.html", {"request": request, "profile": profile.data})


# In resumes or not
_SECTION_PARTIALS = {
    "experience": "profile/partials/experience.html",
    "projects": "profile/partials/projects.html",
    "education": "profile/partials/education.html",
}


@router.post("/{section}/{item_id}/in-resume", response_class=HTMLResponse)
def switch_in_resume(
    request: Request, section: str, item_id: str,
    included: str = Form(""), db: Session = Depends(get_db),
):
    """
    Leave an entry out of resumes, letters and drafted answers, or put it
    back. The entry itself is untouched, so switching it back restores it.
    An unticked checkbox sends nothing, so absent means left out.
    """
    from app.services.profile_service import set_in_documents

    if section not in _SECTION_PARTIALS:
        raise HTTPException(status_code=404, detail="No such section")
    profile = set_in_documents(db, section, item_id, included == "1")
    db.commit()
    return templates.TemplateResponse(
        _SECTION_PARTIALS[section], {"request": request, "profile": profile.data},
    )


def _facts_changed(request: Request, section: str, item_id: str, change) -> HTMLResponse:
    """Apply a bullet_facts change and re-render the section's list, the entry's questions open."""
    if section not in bullet_facts.SECTIONS:
        raise HTTPException(status_code=404, detail="No such section")
    try:
        profile = change()
    except KeyError:
        raise HTTPException(status_code=404, detail="No such entry")
    return templates.TemplateResponse(
        _SECTION_PARTIALS[section],
        {"request": request, "profile": profile.data, "opened": item_id, "panel": "facts"},
    )


@router.post("/{section}/{item_id}/facts", response_class=HTMLResponse)
def add_fact(
    request: Request, section: str, item_id: str,
    about: str = Form(""), answer: str = Form(""), db: Session = Depends(get_db),
):
    """The number a bullet left out, kept on the entry for generation to use."""
    response = _facts_changed(
        request, section, item_id, lambda: bullet_facts.add(db, section, item_id, about, answer))
    db.commit()
    return response


@router.post("/{section}/{item_id}/facts/skip", response_class=HTMLResponse)
def skip_fact(
    request: Request, section: str, item_id: str,
    about: str = Form(""), db: Session = Depends(get_db),
):
    """No number fits this bullet; stop asking."""
    response = _facts_changed(
        request, section, item_id, lambda: bullet_facts.skip(db, section, item_id, about))
    db.commit()
    return response


@router.post("/{section}/{item_id}/facts/{fact_id}/delete", response_class=HTMLResponse)
def delete_fact(
    request: Request, section: str, item_id: str, fact_id: str,
    db: Session = Depends(get_db),
):
    response = _facts_changed(
        request, section, item_id, lambda: bullet_facts.remove(db, section, item_id, fact_id))
    db.commit()
    return response


# Templates
@router.post("/templates", response_class=HTMLResponse)
def save_templates(
    request: Request,
    latex_template: str = Form(""),
    cover_letter_template: str = Form(""),
    db: Session = Depends(get_db),
):
    from app.services.profile_service import save_section
    save_section(db, "latex_template", latex_template)
    profile = save_section(db, "cover_letter_template", cover_letter_template)
    db.commit()
    return templates.TemplateResponse(
        "profile/partials/templates_tab.html",
        {"request": request, "profile": profile.data, "saved": True},
    )


@router.post("/narrative/generate-questions", response_class=HTMLResponse)
def narrative_generate_questions(request: Request, db: Session = Depends(get_db)):
    from app.services.profile_service import generate_questions
    from app.config import settings
    profile = generate_questions(
        db,
        api_key=settings.NVIDIA_NIM_API_KEY,
        base_url=settings.NVIDIA_NIM_BASE_URL,
        model=live().NVIDIA_NIM_MODEL,
    )
    db.commit()
    return templates.TemplateResponse(
        "profile/partials/narrative.html",
        {"request": request, "profile": profile.data},
    )


@router.post("/narrative/answer/{index}", response_class=HTMLResponse)
def save_narrative_answer_route(
    request: Request,
    index: int,
    answer: str = Form(""),
    db: Session = Depends(get_db),
):
    from app.services.profile_service import save_narrative_answer
    profile = save_narrative_answer(db, index=index, answer=answer)
    db.commit()
    item = profile.data["narrative"]["answers"][index]
    return templates.TemplateResponse(
        "profile/partials/narrative_answer.html",
        {"request": request, "item": item, "index": index, "saved": True},
    )


@router.post("/narrative/regenerate-summary", response_class=HTMLResponse)
def regenerate_summary(request: Request, db: Session = Depends(get_db)):
    from app.services.profile_service import generate_summary
    from app.config import settings
    profile = generate_summary(
        db,
        api_key=settings.NVIDIA_NIM_API_KEY,
        base_url=settings.NVIDIA_NIM_BASE_URL,
        model=live().NVIDIA_NIM_MODEL,
    )
    db.commit()
    return templates.TemplateResponse(
        "profile/partials/narrative.html",
        {"request": request, "profile": profile.data},
    )


from fastapi.responses import JSONResponse


@router.get("/seed", response_class=HTMLResponse)
def seed_profile(db: Session = Depends(get_db)):
    """Visit this URL to force-seed profile data."""
    from app.services.profile_service import apply_seed
    apply_seed(db)
    return RedirectResponse(url="/profile?tab=experience", status_code=302)


@router.get("/debug/raw", response_class=JSONResponse)
def debug_profile_raw(db: Session = Depends(get_db)):
    profile = get_or_create_profile(db)
    data = profile.data or {}
    return {
        "has_experience": bool(data.get("experience")),
        "experience_count": len(data.get("experience") or []),
        "skills": data.get("skills"),
        "education_count": len(data.get("education") or []),
        "narrative_summary_len": len((data.get("narrative") or {}).get("summary") or ""),
        "personal_name": (data.get("personal") or {}).get("name"),
    }
