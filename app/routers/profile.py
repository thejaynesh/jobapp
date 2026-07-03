import copy

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.database import get_db
from app.services.locations import REGION_OPTIONS, normalize_prefs
from app.services.profile_service import get_or_create_profile

router = APIRouter(prefix="/profile", tags=["profile"])
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["region_options"] = REGION_OPTIONS
templates.env.globals["location_prefs"] = normalize_prefs

TABS = ["personal", "experience", "projects", "skills", "education", "templates", "narrative"]


@router.get("", response_class=HTMLResponse)
def get_profile(request: Request, tab: str = "personal", db: Session = Depends(get_db)):
    if tab not in TABS:
        tab = "personal"
    profile = get_or_create_profile(db)
    db.commit()
    return templates.TemplateResponse(
        "profile/index.html",
        {"request": request, "profile": profile.data, "active_tab": tab},
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
@router.post("/skills", response_class=HTMLResponse)
def save_skills(
    request: Request,
    languages: str = Form(""), frameworks: str = Form(""),
    tools: str = Form(""), clouds: str = Form(""),
    target_roles: str = Form(""),
    location_regions: list[str] = Form(default=[]),
    remote_ok: str = Form(""), custom_locations: str = Form(""),
    excluded_companies: str = Form(""), min_match_score: int = Form(70),
    db: Session = Depends(get_db),
):
    from app.services.locations import REGIONS, search_locations
    from app.services.profile_service import save_section
    save_section(db, "skills", {
        "languages": [x.strip() for x in languages.split(",") if x.strip()],
        "frameworks": [x.strip() for x in frameworks.split(",") if x.strip()],
        "tools": [x.strip() for x in tools.split(",") if x.strip()],
        "clouds": [x.strip() for x in clouds.split(",") if x.strip()],
    })
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
    profile = save_section(db, "min_match_score", min_match_score)
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
        model=settings.NVIDIA_NIM_MODEL,
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
        model=settings.NVIDIA_NIM_MODEL,
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
