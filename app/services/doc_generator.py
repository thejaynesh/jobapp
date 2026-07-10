import copy
import json
import logging
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from app.config import settings
from app.services.matcher import chat_completion

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).parent.parent / "templates" / "latex"
_OUTPUT_DIR = Path(settings.DOCS_OUTPUT_DIR)


# ---------------------------------------------------------------------------
# LaTeX escaping
# ---------------------------------------------------------------------------

_LATEX_SPECIAL = re.compile(r'([\\&%#$_{}^~])')
_LATEX_MAP = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "#": r"\#",
    "$": r"\$",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "^": r"\^{}",
    "~": r"\textasciitilde{}",
}


_UNICODE_MAP = {
    "—": "---",       # em dash
    "–": "--",        # en dash
    "‘": "`",         # left single quote
    "’": "'",         # right single quote / apostrophe
    "“": "``",        # left double quote
    "”": "''",        # right double quote
    "•": r"\textbullet{}",   # bullet
    "…": "...",       # ellipsis
    " ": " ",         # non-breaking space
}
_UNICODE_RE = re.compile("[" + "".join(_UNICODE_MAP.keys()) + "]")


def latex_escape(text) -> str:
    if not isinstance(text, str):
        return ""
    text = _LATEX_SPECIAL.sub(lambda m: _LATEX_MAP[m.group()], text)
    return _UNICODE_RE.sub(lambda m: _UNICODE_MAP[m.group()], text)


def _make_jinja_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=False,
    )
    env.filters["latex_escape"] = latex_escape
    return env


# ---------------------------------------------------------------------------
# Context builders
# ---------------------------------------------------------------------------

def _ensure_url(value: str) -> str:
    if value and not value.startswith(("http://", "https://")):
        return "https://" + value
    return value


def _normalize_profile_for_template(profile_data: dict) -> dict:
    """Map stored profile format → shape LaTeX templates expect."""
    personal = profile_data.get("personal") or {}
    return {
        "name": personal.get("name") or "",
        "contact": {
            "email": personal.get("email") or "",
            "phone": personal.get("phone") or "",
            "location": personal.get("location") or "",
            "linkedin": _ensure_url(personal.get("linkedin") or ""),
            "github": _ensure_url(personal.get("github") or ""),
            "website": _ensure_url(personal.get("website") or ""),
        },
    }


def _normalize_experience(experience_list: list, tailored_bullets: list[dict] | None) -> list:
    """Normalize experience items and apply tailored bullets."""
    bullet_map = {}
    if tailored_bullets:
        for e in tailored_bullets:
            bullet_map[(e.get("company") or "", e.get("title") or "")] = e.get("bullets", [])

    result = []
    for exp in experience_list:
        e = dict(exp)
        # stored as "role", templates expect "title"
        if not e.get("title"):
            e["title"] = e.get("role") or ""
        key = (e.get("company") or "", e.get("title") or "")
        if key in bullet_map:
            e["bullets"] = bullet_map[key]
        result.append(e)
    return result


def _normalize_education(education_list: list) -> list:
    """Normalize education items: add graduation_year from end_date."""
    result = []
    for edu in education_list:
        e = dict(edu)
        if not e.get("graduation_year"):
            e["graduation_year"] = e.get("end_date") or ""
        result.append(e)
    return result


def _score_project_keywords(project: dict, job_description: str) -> int:
    """Return number of project tech keywords found in the job description (case-insensitive)."""
    if not job_description:
        return 0
    jd_lower = job_description.lower()
    tech_terms = project.get("tech") or []
    name_tokens = re.findall(r'\w+', (project.get("name") or "").lower())
    all_terms = [t.lower() for t in tech_terms] + name_tokens
    return sum(1 for t in all_terms if t and len(t) > 1 and t in jd_lower)


def _score_experience_keywords(exp: dict, job_description: str) -> int:
    """Return number of experience tech/title keywords found in the job description."""
    if not job_description:
        return 0
    jd_lower = job_description.lower()
    title = exp.get("title") or exp.get("role") or ""
    terms = [t.lower() for t in (exp.get("tech") or [])]
    terms += re.findall(r'\w+', title.lower())
    return sum(1 for t in terms if t and len(t) > 1 and t in jd_lower)


def _strip_json_fences(raw: str) -> str:
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _fallback_selection(
    experiences: list[dict],
    projects: list[dict],
    skills: dict,
    job_description: str,
    max_experience: int,
    max_projects: int,
) -> dict:
    """Keyword-only selection used when the LLM is unavailable or returns junk."""
    exp_scored = sorted(
        experiences, key=lambda e: _score_experience_keywords(e, job_description), reverse=True
    )
    kept_exp = [e for e in exp_scored if _score_experience_keywords(e, job_description) > 0]
    # Always keep at least the top roles so the resume is never empty.
    if not kept_exp:
        kept_exp = experiences[:max_experience]
    kept_exp = kept_exp[:max_experience]

    proj_scored = sorted(
        projects, key=lambda p: _score_project_keywords(p, job_description), reverse=True
    )
    kept_proj = [p for p in proj_scored if _score_project_keywords(p, job_description) > 0]
    if not kept_proj:
        kept_proj = projects
    kept_proj = kept_proj[:max_projects]

    return {"experience": kept_exp, "projects": kept_proj, "skills": skills}


def tailor_resume_selection(
    profile_data: dict,
    job_title: str,
    job_description: str,
    api_key: str,
    base_url: str,
    model: str,
    max_experience: int = 3,
    max_projects: int = 2,
) -> dict:
    """
    Curate the resume to a single page by selecting only the content relevant to a
    specific job. Returns {"experience": [...], "projects": [...], "skills": {...}}.

    Two layers, mirroring project selection:
    1. Keyword grounding: every experience/project is scored against the JD so the
       LLM is told which items actually overlap (and the fallback path needs no LLM).
    2. LLM curation: from the REAL items only (referenced by id), the model picks the
       relevant experiences and projects and trims the skills to what fits the role —
       dropping tangential entries (e.g. unrelated volunteer roles) to save space.

    Grounding guarantees: only ids/skills present in the profile survive, so the model
    cannot invent experience, projects, or skills.
    """
    experiences = profile_data.get("experience") or []
    projects = profile_data.get("projects") or []
    skills = profile_data.get("skills") or {}

    # Nothing to curate / no JD to curate against → keep everything.
    if not job_description or (not experiences and not projects):
        return {"experience": experiences, "projects": projects, "skills": skills}

    valid_exp_ids = {e.get("id") for e in experiences if e.get("id")}
    valid_proj_ids = {p.get("id") for p in projects if p.get("id")}

    exp_summary = [
        {
            "id": e.get("id"),
            "title": e.get("title") or e.get("role") or "",
            "company": e.get("company") or "",
            "dates": f"{e.get('start_date', '')} - {e.get('end_date', '')}".strip(" -"),
            "tech": e.get("tech") or [],
            "keyword_score": _score_experience_keywords(e, job_description),
        }
        for e in experiences
    ]
    proj_summary = [
        {
            "id": p.get("id"),
            "name": p.get("name") or "",
            "tech": p.get("tech") or [],
            "keyword_score": _score_project_keywords(p, job_description),
        }
        for p in projects
    ]

    messages = [
        {
            "role": "system",
            "content": (
                "You are a resume editor. Your goal is a focused, ONE-PAGE resume tailored "
                "to a specific job. From the candidate's real items, select only what is "
                "relevant and trim the rest to save space.\n"
                "Rules:\n"
                f"- Pick up to {max_experience} of the most relevant experiences. You MAY drop "
                "tangential roles (e.g. unrelated volunteer or club positions) entirely, but "
                "keep substantial professional roles unless clearly irrelevant. Never return zero.\n"
                f"- Pick up to {max_projects} of the most relevant projects.\n"
                "- For skills, keep only those relevant to THIS job; preserve the category keys "
                "and drop skills that don't help for this role. Use only skills from the input.\n"
                "- 'keyword_score' tells you how many of an item's terms already appear in the job "
                "description; higher means more obviously relevant, but use judgement.\n"
                "- Use ONLY ids and skills that appear in the input. Do not invent anything.\n"
                "Return ONLY JSON of the form: "
                '{"experience_ids": ["..."], "project_ids": ["..."], '
                '"skills": {"category": ["skill", ...]}}'
            ),
        },
        {
            "role": "user",
            "content": (
                f"Job title: {job_title}\n"
                f"Job description (excerpt):\n{job_description[:2000]}\n\n"
                f"Experiences:\n{json.dumps(exp_summary, indent=2)}\n\n"
                f"Projects:\n{json.dumps(proj_summary, indent=2)}\n\n"
                f"Skills:\n{json.dumps(skills, indent=2)}"
            ),
        },
    ]

    try:
        raw = chat_completion(messages=messages, api_key=api_key, base_url=base_url, model=model)
        parsed = json.loads(_strip_json_fences(raw))

        # Ground experience selection against real ids, preserving profile order.
        sel_exp_ids = {i for i in (parsed.get("experience_ids") or []) if i in valid_exp_ids}
        selected_exp = [e for e in experiences if e.get("id") in sel_exp_ids]
        if not selected_exp:
            selected_exp = experiences[:max_experience]

        # Ground project selection.
        sel_proj_ids = {i for i in (parsed.get("project_ids") or []) if i in valid_proj_ids}
        selected_proj = [p for p in projects if p.get("id") in sel_proj_ids]
        if not selected_proj and projects:
            selected_proj = _fallback_selection(
                experiences, projects, skills, job_description, max_experience, max_projects
            )["projects"]
        selected_proj = selected_proj[:max_projects]

        # Ground skills: keep only skills that exist in the profile, preserve categories.
        curated_skills = {}
        llm_skills = parsed.get("skills") or {}
        for category, original_items in skills.items():
            picked = llm_skills.get(category)
            if isinstance(picked, list) and picked:
                original_lower = {s.lower(): s for s in original_items}
                kept = [original_lower[s.lower()] for s in picked if s.lower() in original_lower]
                curated_skills[category] = kept if kept else original_items
            else:
                # Category omitted/empty by the model → keep original to be safe.
                curated_skills[category] = original_items
        if not curated_skills:
            curated_skills = skills

        return {"experience": selected_exp, "projects": selected_proj, "skills": curated_skills}
    except Exception as exc:
        logger.error("tailor_resume_selection LLM error: %s", exc)
        return _fallback_selection(
            experiences, projects, skills, job_description, max_experience, max_projects
        )


def build_resume_context(
    profile_data: dict,
    tailored_bullets: list[dict] | None,
    selected_experience: list[dict] | None = None,
    selected_projects: list[dict] | None = None,
    selected_skills: dict | None = None,
) -> dict:
    experience_source = (
        selected_experience if selected_experience is not None
        else (profile_data.get("experience") or [])
    )
    return {
        "profile": _normalize_profile_for_template(profile_data),
        "narrative_summary": (profile_data.get("narrative") or {}).get("summary", ""),
        "skills": selected_skills if selected_skills is not None else (profile_data.get("skills") or {}),
        "experience": _normalize_experience(experience_source, tailored_bullets),
        "education": _normalize_education(profile_data.get("education") or []),
        "projects": selected_projects if selected_projects is not None else (profile_data.get("projects") or []),
    }


def build_cover_letter_context(profile_data: dict, job_company: str, job_title: str, body: str) -> dict:
    return {
        "profile": _normalize_profile_for_template(profile_data),
        "job_company": job_company,
        "job_title": job_title,
        "cover_letter_body": body,
    }


# ---------------------------------------------------------------------------
# Render + compile
# ---------------------------------------------------------------------------

class DocGenerationError(Exception):
    pass


def render_latex(template_name: str, context: dict) -> str:
    env = _make_jinja_env()
    template = env.get_template(template_name)
    return template.render(**context)


_PAGES_RE = re.compile(r"Output written on .*?\((\d+) pages?")


def compile_pdf(tex_source: str, output_path: Path, page_count_out: dict | None = None) -> Path:
    with tempfile.TemporaryDirectory() as tmpdir:
        tex_file = Path(tmpdir) / "document.tex"
        tex_file.write_text(tex_source, encoding="utf-8")
        result = subprocess.run(
            [
                "pdflatex",
                "-interaction=nonstopmode",
                "-halt-on-error",
                "-output-directory", tmpdir,
                str(tex_file),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            log = (result.stdout or "") + (result.stderr or "")
            error_lines = [l for l in log.splitlines() if l.startswith("!")]
            detail = "\n".join(error_lines[:5]) if error_lines else log[-800:]
            raise DocGenerationError(
                f"pdflatex failed (exit {result.returncode}):\n{detail}"
            )
        if page_count_out is not None:
            m = _PAGES_RE.search(result.stdout or "")
            if m:
                page_count_out["pages"] = int(m.group(1))
        compiled = Path(tmpdir) / "document.pdf"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(str(compiled), str(output_path))
    return output_path


def _trim_resume_context(ctx: dict, level: int) -> dict:
    """Return a progressively trimmed copy of a resume context.

    Level 0 = untouched. Each level cuts lower-priority content (bullets are
    assumed strongest-first, so they are trimmed from the end):
      1: cap bullets at 3 per experience / 2 per project
      2: cap bullets at 2 per experience, keep only the top project
      3: drop the summary, cap project bullets at 1
    """
    if level <= 0:
        return ctx
    trimmed = copy.deepcopy(ctx)
    max_exp_bullets = {1: 3, 2: 2, 3: 2}[min(level, 3)]
    max_proj_bullets = {1: 2, 2: 2, 3: 1}[min(level, 3)]
    for e in trimmed.get("experience") or []:
        e["bullets"] = (e.get("bullets") or [])[:max_exp_bullets]
    if level >= 2:
        trimmed["projects"] = (trimmed.get("projects") or [])[:1]
    if level >= 3:
        trimmed["narrative_summary"] = ""
    for p in trimmed.get("projects") or []:
        p["bullets"] = (p.get("bullets") or [])[:max_proj_bullets]
    return trimmed


def compile_resume_one_page(resume_ctx: dict, output_path: Path) -> Path:
    """Compile the resume, re-trimming and recompiling until it fits one page.

    If even the most aggressive trim level still overflows, the last (smallest)
    version is kept rather than failing generation.
    """
    for level in range(4):
        ctx = _trim_resume_context(resume_ctx, level)
        tex = render_latex("resume.tex.j2", ctx)
        pages: dict = {}
        compile_pdf(tex, output_path, page_count_out=pages)
        if pages.get("pages", 1) <= 1:
            if level:
                logger.info("Resume fit to one page at trim level %d", level)
            return output_path
        logger.info("Resume is %s pages at trim level %d — trimming further", pages.get("pages"), level)
    logger.warning("Resume still over one page after maximum trimming")
    return output_path


# ---------------------------------------------------------------------------
# LLM tailoring
# ---------------------------------------------------------------------------

def generate_cover_letter_body(
    profile_data: dict,
    job_company: str,
    job_title: str,
    job_description: str,
    api_key: str,
    base_url: str,
    model: str,
) -> str:
    skills = profile_data.get("skills", {})
    skills_flat = [s for cat in skills.values() for s in cat]
    summary = (profile_data.get("narrative") or {}).get("summary", "")
    personal = profile_data.get("personal") or {}
    name = personal.get("name") or "Candidate"
    experience = profile_data.get("experience", [])
    exp_summary = "; ".join(
        f"{e.get('role') or e.get('title', '')} at {e.get('company', '')}"
        for e in experience[:3]
    )

    messages = [
        {
            "role": "system",
            "content": (
                "You write professional, concise cover letter bodies (3 paragraphs, ~200 words). "
                "Write in first person. No salutation or sign-off — body text only. "
                "Be specific about the candidate's relevant skills and experience."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Candidate: {name}\nSummary: {summary}\n"
                f"Skills: {', '.join(skills_flat)}\nExperience: {exp_summary}\n\n"
                f"Job: {job_title} at {job_company}\n"
                f"Description:\n{job_description[:1500]}"
            ),
        },
    ]
    try:
        return chat_completion(messages=messages, api_key=api_key, base_url=base_url, model=model)
    except Exception as exc:
        logger.error("generate_cover_letter_body LLM error: %s", exc)
        return (
            f"I am excited to apply for the {job_title} position at {job_company}. "
            f"My background in {', '.join(skills_flat[:3])} aligns well with your requirements. "
            "I look forward to discussing how I can contribute to your team."
        )


def _parse_bullets_response(content: str) -> list[dict]:
    text = content.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text.strip())


def tailor_resume_bullets(
    profile_data: dict,
    job_title: str,
    job_description: str,
    api_key: str,
    base_url: str,
    model: str,
) -> list[dict]:
    experience = profile_data.get("experience", [])
    exp_json = [
        {"company": e.get("company"), "title": e.get("title") or e.get("role") or "", "bullets": e.get("bullets", [])}
        for e in experience
    ]
    messages = [
        {
            "role": "system",
            "content": (
                "You are a resume writer. Given a candidate's experience entries and a job description, "
                "rewrite the bullet points to highlight the most relevant accomplishments. "
                "Return a JSON array with the SAME structure: "
                '[{"company": str, "title": str, "bullets": [str, ...]}]. '
                "Keep bullet count the same. Return ONLY the JSON array."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Job title: {job_title}\nDescription:\n{job_description[:1500]}\n\n"
                f"Experience entries:\n{json.dumps(exp_json, indent=2)}"
            ),
        },
    ]
    try:
        raw = chat_completion(messages=messages, api_key=api_key, base_url=base_url, model=model)
        return _parse_bullets_response(raw)
    except Exception as exc:
        logger.error("tailor_resume_bullets error: %s", exc)
        return []


# ---------------------------------------------------------------------------
# DB version management + orchestrator
# ---------------------------------------------------------------------------

def _next_version(db, application_id: uuid.UUID, doc_type) -> int:
    from app.models.application import ApplicationDocument
    count = (
        db.query(ApplicationDocument)
        .filter(
            ApplicationDocument.application_id == application_id,
            ApplicationDocument.doc_type == doc_type,
        )
        .count()
    )
    return count + 1


def _set_only_current(db, application_id: uuid.UUID, doc_type, new_doc) -> None:
    from app.models.application import ApplicationDocument
    old_docs = (
        db.query(ApplicationDocument)
        .filter(
            ApplicationDocument.application_id == application_id,
            ApplicationDocument.doc_type == doc_type,
        )
        .all()
    )
    for doc in old_docs:
        doc.is_current = False
    new_doc.is_current = True


def generate_documents(db, application, feedback: str | None = None) -> None:
    from app.models.application import ApplicationDocument, DocType
    from app.models.job import JobStatus
    from app.models.profile import Profile

    api_key = settings.NVIDIA_NIM_API_KEY
    base_url = settings.NVIDIA_NIM_BASE_URL
    model = settings.NVIDIA_NIM_MODEL

    profile = db.query(Profile).first()
    profile_data = profile.data if profile else {}
    job = application.job

    # Curate which experiences, projects, and skills to include so the resume
    # stays focused (ideally one page) and relevant to this specific job.
    selection = tailor_resume_selection(
        profile_data, job.title, job.description or "", api_key, base_url, model
    )
    selected_experience = selection["experience"]
    selected_projects = selection["projects"]
    selected_skills = selection["skills"]

    # Rewrite bullets only for the experiences we are actually keeping.
    bullet_profile = {**profile_data, "experience": selected_experience}
    tailored_bullets = tailor_resume_bullets(
        bullet_profile, job.title, job.description or "", api_key, base_url, model
    )
    cover_body = generate_cover_letter_body(
        profile_data, job.company, job.title, job.description or "", api_key, base_url, model
    )

    # Resume
    resume_ctx = build_resume_context(
        profile_data,
        tailored_bullets if tailored_bullets else None,
        selected_experience=selected_experience,
        selected_projects=selected_projects,
        selected_skills=selected_skills,
    )
    resume_version = _next_version(db, application.id, DocType.resume)
    resume_filename = f"{application.id}_resume_v{resume_version}.pdf"
    resume_path = _OUTPUT_DIR / str(application.id) / resume_filename
    compiled_resume = compile_resume_one_page(resume_ctx, resume_path)

    resume_doc = ApplicationDocument(
        application_id=application.id,
        doc_type=DocType.resume,
        version=resume_version,
        path=str(compiled_resume),
        generation_feedback=feedback,
    )
    _set_only_current(db, application.id, DocType.resume, resume_doc)
    db.add(resume_doc)

    # Cover letter
    cl_ctx = build_cover_letter_context(profile_data, job.company, job.title, cover_body)
    cl_tex = render_latex("cover_letter.tex.j2", cl_ctx)
    cl_version = _next_version(db, application.id, DocType.cover_letter)
    cl_filename = f"{application.id}_cover_letter_v{cl_version}.pdf"
    cl_path = _OUTPUT_DIR / str(application.id) / cl_filename
    compiled_cl = compile_pdf(cl_tex, cl_path)

    cl_doc = ApplicationDocument(
        application_id=application.id,
        doc_type=DocType.cover_letter,
        version=cl_version,
        path=str(compiled_cl),
        generation_feedback=feedback,
    )
    _set_only_current(db, application.id, DocType.cover_letter, cl_doc)
    db.add(cl_doc)

    job.status = JobStatus.docs_generated
    db.commit()
