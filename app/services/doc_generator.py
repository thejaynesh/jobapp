import copy
import json
import logging
import re
import shutil
import subprocess
import tempfile
import unicodedata
import uuid
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from app.config import settings
# Quality-first multi-provider chat (Anthropic -> Gemini -> passed-in primary);
# keeps the single-provider chat_completion signature.
from app.llm.providers import generation_chat as chat_completion

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
    "→": " to ",      # rightwards arrow
    "×": "x",         # multiplication sign
    " ": " ",         # non-breaking space
}
_UNICODE_RE = re.compile("[" + "".join(_UNICODE_MAP.keys()) + "]")
_NON_ASCII_RE = re.compile(r"[^\x00-\x7F]")


def _fold_non_ascii(match: re.Match) -> str:
    """Fold an unmapped non-ASCII char to its closest ASCII form, or drop it.

    pdflatex aborts on Unicode it has no declaration for (e.g. U+272A), and
    LLM output can contain anything — so every char must leave here compilable.
    NFKD strips accents (e.g. an accented e becomes plain e); symbols with no
    ASCII equivalent become ''. Folded output is re-escaped in case the fold
    produced a LaTeX special character.
    """
    folded = unicodedata.normalize("NFKD", match.group()).encode("ascii", "ignore").decode()
    return _LATEX_SPECIAL.sub(lambda m: _LATEX_MAP[m.group()], folded)


def latex_escape(text) -> str:
    if not isinstance(text, str):
        return ""
    text = _LATEX_SPECIAL.sub(lambda m: _LATEX_MAP[m.group()], text)
    text = _UNICODE_RE.sub(lambda m: _UNICODE_MAP[m.group()], text)
    return _NON_ASCII_RE.sub(_fold_non_ascii, text)


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


# ---------------------------------------------------------------------------
# Job insights (shared grounding for summary, bullets, and cover letter)
# ---------------------------------------------------------------------------

def _profile_terms(profile_data: dict) -> set[str]:
    terms: set[str] = set()
    for items in (profile_data.get("skills") or {}).values():
        terms.update(items or [])
    for e in profile_data.get("experience") or []:
        terms.update(e.get("tech") or [])
    for p in profile_data.get("projects") or []:
        terms.update(p.get("tech") or [])
    return {t for t in terms if t}


def _fallback_insights(profile_data: dict, job_description: str) -> dict:
    """Keyword-only insights: profile terms that literally appear in the JD."""
    jd_lower = (job_description or "").lower()
    keywords = sorted(
        {t for t in _profile_terms(profile_data) if t.lower() in jd_lower},
        key=str.lower,
    )
    return {"keywords": keywords[:15], "requirements": [], "company_signals": []}


def extract_job_insights(
    profile_data: dict,
    job_title: str,
    job_company: str,
    job_description: str,
    api_key: str,
    base_url: str,
    model: str,
) -> dict:
    """
    One analysis pass over the JD that grounds all downstream generation:
      keywords         — ATS terms the resume should contain (skills, tools, methods)
      requirements     — the job's most important hard requirements, ranked
      company_signals  — company/product/mission specifics usable in a cover letter hook
    Falls back to profile-term matching if the LLM is unavailable.
    """
    if not job_description:
        return {"keywords": [], "requirements": [], "company_signals": []}

    messages = [
        {
            "role": "system",
            "content": (
                "You analyze job descriptions for resume targeting. Return ONLY a JSON "
                "object with these fields:\n"
                '  "keywords": up to 15 exact terms an ATS would scan for (technologies, '
                "tools, methodologies), most important first, using the JD's own spelling,\n"
                '  "requirements": up to 6 of the job\'s most important requirements or '
                "responsibilities, each a short phrase, most important first,\n"
                '  "company_signals": up to 4 short phrases about the company, product, '
                "team, or mission stated in the JD that a cover letter could reference.\n"
                "Only use information present in the job description. No markdown."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Job title: {job_title}\n"
                f"Company: {job_company}\n"
                f"Description:\n{job_description[:4000]}"
            ),
        },
    ]
    try:
        raw = chat_completion(messages=messages, api_key=api_key, base_url=base_url, model=model)
        parsed = json.loads(_strip_json_fences(raw))
        insights = {
            "keywords": [str(k).strip() for k in (parsed.get("keywords") or []) if str(k).strip()][:15],
            "requirements": [str(r).strip() for r in (parsed.get("requirements") or []) if str(r).strip()][:6],
            "company_signals": [str(c).strip() for c in (parsed.get("company_signals") or []) if str(c).strip()][:4],
        }
        if not insights["keywords"]:
            insights["keywords"] = _fallback_insights(profile_data, job_description)["keywords"]
        return insights
    except Exception as exc:
        logger.error("extract_job_insights LLM error: %s", exc)
        return _fallback_insights(profile_data, job_description)


def _keyword_coverage(resume_ctx: dict, keywords: list[str]) -> tuple[list[str], list[str]]:
    """Which JD keywords made it into the final resume content (ATS coverage check)."""
    parts: list[str] = [resume_ctx.get("narrative_summary") or ""]
    for items in (resume_ctx.get("skills") or {}).values():
        parts.extend(items or [])
    for e in resume_ctx.get("experience") or []:
        parts.append(e.get("title") or "")
        parts.extend(e.get("bullets") or [])
    for p in resume_ctx.get("projects") or []:
        parts.append(p.get("name") or "")
        parts.append(p.get("description") or "")
        parts.extend(p.get("bullets") or [])
    text = " ".join(parts).lower()
    present = [k for k in keywords if k.lower() in text]
    missing = [k for k in keywords if k.lower() not in text]
    return present, missing


def _restore_missing_skills(
    profile_skills: dict, selected_skills: dict, missing_keywords: list[str]
) -> dict:
    """
    Re-add real profile skills that the curation step trimmed but that the JD
    scans for. Only skills already in the profile are restored — this never
    introduces a skill the candidate doesn't claim.
    """
    missing_lower = {k.lower() for k in missing_keywords}
    result = {cat: list(items or []) for cat, items in (selected_skills or {}).items()}
    for cat, items in (profile_skills or {}).items():
        for skill in items or []:
            if skill.lower() in missing_lower:
                bucket = result.setdefault(cat, [])
                if skill not in bucket:
                    bucket.append(skill)
    return result


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


def tailor_summary(
    profile_data: dict,
    job_title: str,
    job_company: str,
    insights: dict | None,
    api_key: str,
    base_url: str,
    model: str,
    feedback: str | None = None,
) -> str:
    """
    Rewrite the profile's narrative summary so the top of the resume speaks to THIS
    job instead of being identical on every application. Falls back to the stored
    summary if the LLM is unavailable or returns junk.
    """
    base_summary = (profile_data.get("narrative") or {}).get("summary", "")
    if not base_summary:
        return base_summary

    keywords = (insights or {}).get("keywords") or []
    requirements = (insights or {}).get("requirements") or []

    system_content = (
        "You rewrite a candidate's resume summary to target a specific job.\n"
        "Rules:\n"
        "- 2-3 sentences, first person, no filler ('passionate', 'results-driven', "
        "'team player') and no addressing the company directly.\n"
        "- Keep every claim from the original summary truthful; you may drop parts "
        "irrelevant to this job, but NEVER add experience, employers, or credentials "
        "that are not in the original.\n"
        "- Naturally weave in the job's terminology where the original summary "
        "supports it.\n"
        "Return ONLY the rewritten summary text."
    )
    user_content = (
        f"Original summary:\n{base_summary}\n\n"
        f"Target job: {job_title} at {job_company}\n"
        + (f"Job keywords: {', '.join(keywords)}\n" if keywords else "")
        + (f"Job requirements: {'; '.join(requirements)}\n" if requirements else "")
        + (f"\nUser feedback on the previous version (must address): {feedback}\n" if feedback else "")
    )
    try:
        raw = chat_completion(
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content},
            ],
            api_key=api_key, base_url=base_url, model=model,
            temperature=0.4,
        )
        summary = raw.strip().strip('"')
        # Sanity bounds: a summary should be a short paragraph, not empty or an essay.
        if 40 <= len(summary) <= 800:
            return summary
        logger.warning("tailor_summary: rejected output of length %d", len(summary))
        return base_summary
    except Exception as exc:
        logger.error("tailor_summary LLM error: %s", exc)
        return base_summary


# Canonical display order and labels for skill categories. JSONB storage
# alphabetizes dict keys, so without this the PDF prints them in random order
# with raw keys like "Ai_ml".
_SKILL_CATEGORY_ORDER = ["languages", "frameworks", "ai_ml", "databases", "clouds", "tools"]
_SKILL_CATEGORY_LABELS = {
    "languages": "Languages",
    "frameworks": "Frameworks",
    "ai_ml": "AI/ML",
    "databases": "Databases",
    "clouds": "Cloud",
    "tools": "Tools",
}


def _ordered_skills(skills: dict) -> list[tuple[str, list]]:
    """Return (label, items) pairs in canonical order, unknown categories last."""
    ordered = []
    for key in _SKILL_CATEGORY_ORDER:
        items = skills.get(key)
        if items:
            ordered.append((_SKILL_CATEGORY_LABELS[key], items))
    for key, items in skills.items():
        if key not in _SKILL_CATEGORY_ORDER and items:
            ordered.append((key.replace("_", " ").title(), items))
    return ordered


def build_resume_context(
    profile_data: dict,
    tailored_bullets: list[dict] | None,
    selected_experience: list[dict] | None = None,
    selected_projects: list[dict] | None = None,
    selected_skills: dict | None = None,
    tailored_summary: str | None = None,
) -> dict:
    experience_source = (
        selected_experience if selected_experience is not None
        else (profile_data.get("experience") or [])
    )
    skills = selected_skills if selected_skills is not None else (profile_data.get("skills") or {})
    return {
        "profile": _normalize_profile_for_template(profile_data),
        "narrative_summary": tailored_summary or (profile_data.get("narrative") or {}).get("summary", ""),
        "skills": skills,
        "skills_ordered": _ordered_skills(skills),
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


def compile_pdf_with_pages(tex_source: str, output_path: Path) -> tuple[Path, int]:
    """Compile LaTeX to PDF; returns (path, page_count) parsed from the pdflatex log."""
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
        m = _PAGES_RE.search(result.stdout or "")
        pages = int(m.group(1)) if m else 1
        compiled = Path(tmpdir) / "document.pdf"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(str(compiled), str(output_path))
    return output_path, pages


def compile_pdf(tex_source: str, output_path: Path) -> Path:
    return compile_pdf_with_pages(tex_source, output_path)[0]


def _trim_resume_context(ctx: dict, level: int) -> dict:
    """Return a progressively trimmed copy of a resume context.

    Level 0 = untouched. Each level cuts lower-priority content (bullets are
    assumed strongest-first, so they are trimmed from the end; experiences and
    projects are assumed most-relevant-first, so they are dropped from the end):
      1: cap bullets at 3 per experience / 2 per project
      2: cap bullets at 2 per experience, keep only the top project
      3: drop the summary, cap project bullets at 1
      4: keep only the top 2 experiences
    """
    if level <= 0:
        return ctx
    trimmed = copy.deepcopy(ctx)
    max_exp_bullets = 3 if level == 1 else 2
    max_proj_bullets = 2 if level < 3 else 1
    for e in trimmed.get("experience") or []:
        e["bullets"] = (e.get("bullets") or [])[:max_exp_bullets]
    if level >= 2:
        trimmed["projects"] = (trimmed.get("projects") or [])[:1]
    if level >= 3:
        trimmed["narrative_summary"] = ""
    if level >= 4:
        trimmed["experience"] = (trimmed.get("experience") or [])[:2]
    for p in trimmed.get("projects") or []:
        p["bullets"] = (p.get("bullets") or [])[:max_proj_bullets]
    return trimmed


def compile_resume_one_page(resume_ctx: dict, output_path: Path) -> Path:
    """Compile the resume, re-trimming and recompiling until it fits one page.

    If even the most aggressive trim level still overflows, the last (smallest)
    version is kept rather than failing generation.
    """
    for level in range(5):
        ctx = _trim_resume_context(resume_ctx, level)
        tex = render_latex("resume.tex.j2", ctx)
        _, pages = compile_pdf_with_pages(tex, output_path)
        if pages <= 1:
            if level:
                logger.info("Resume fit to one page at trim level %d", level)
            return output_path
        logger.info("Resume is %d pages at trim level %d — trimming further", pages, level)
    logger.warning("Resume still over one page after maximum trimming")
    return output_path


# ---------------------------------------------------------------------------
# LLM tailoring
# ---------------------------------------------------------------------------

_COVER_LETTER_BANNED = [
    "I am excited to apply",
    "I am writing to express",
    "I believe I would be a great fit",
    "passionate",
    "team player",
    "fast learner",
    "results-driven",
    "esteemed",
    "perfect fit",
    "aligns perfectly",
]


def _evidence_block(experience: list[dict], projects: list[dict]) -> str:
    """Concrete accomplishments the letter is allowed to draw from."""
    lines: list[str] = []
    for e in experience[:3]:
        role = e.get("role") or e.get("title") or ""
        lines.append(f"EXPERIENCE — {role} at {e.get('company', '')}:")
        lines.extend(f"  - {b}" for b in (e.get("bullets") or [])[:3])
    for p in projects[:2]:
        lines.append(f"PROJECT — {p.get('name', '')} ({p.get('description', '')}):")
        lines.extend(f"  - {b}" for b in (p.get("bullets") or [])[:3])
    return "\n".join(lines)


def generate_cover_letter_body(
    profile_data: dict,
    job_company: str,
    job_title: str,
    job_description: str,
    api_key: str,
    base_url: str,
    model: str,
    insights: dict | None = None,
    feedback: str | None = None,
    selected_experience: list[dict] | None = None,
    selected_projects: list[dict] | None = None,
) -> str:
    skills = profile_data.get("skills", {})
    skills_flat = [s for cat in skills.values() for s in cat]
    summary = (profile_data.get("narrative") or {}).get("summary", "")
    personal = profile_data.get("personal") or {}
    name = personal.get("name") or "Candidate"
    experience = (
        selected_experience if selected_experience
        else profile_data.get("experience", [])
    )
    projects = (
        selected_projects if selected_projects
        else profile_data.get("projects", [])
    )
    evidence = _evidence_block(experience, projects)
    requirements = (insights or {}).get("requirements") or []
    company_signals = (insights or {}).get("company_signals") or []

    system_content = (
        "You write cover letter bodies that read like a specific person wrote them for "
        "a specific job — not a template. Structure (3 paragraphs, 180-250 words total):\n"
        "1. Hook: name the role, and open with the single strongest reason this candidate "
        "fits — a concrete accomplishment or a specific connection to what the company is "
        "building. Never open with a generic statement of interest.\n"
        "2. Evidence: pick the 2-3 job requirements the candidate can best prove, and back "
        "each with a specific accomplishment (with its metric) from the evidence list. "
        "Connect each accomplishment to what the job needs, don't just restate it.\n"
        "3. Close: one or two sentences on what the candidate would bring to this team, "
        "then a brief forward-looking line.\n"
        "Hard rules:\n"
        "- First person, plain confident tone, contractions are fine.\n"
        "- Use ONLY accomplishments from the evidence list; never invent employers, "
        "numbers, or technologies. It's fine to mention at most one honest gap-bridging "
        "skill (e.g. 'deep Java experience maps directly to Kotlin').\n"
        f"- Never use these phrases or close variants: {', '.join(_COVER_LETTER_BANNED)}.\n"
        "- No salutation, no sign-off, no bullet points — body paragraphs only."
    )

    user_content = (
        f"Candidate: {name}\n"
        f"Candidate summary: {summary}\n"
        f"Skills: {', '.join(skills_flat)}\n\n"
        f"Evidence list (the ONLY accomplishments you may cite):\n{evidence}\n\n"
        f"Job: {job_title} at {job_company}\n"
        + (f"Top job requirements: {'; '.join(requirements)}\n" if requirements else "")
        + (f"Company signals from the JD: {'; '.join(company_signals)}\n" if company_signals else "")
        + f"Job description (excerpt):\n{job_description[:2500]}\n"
        + (f"\nUser feedback on the previous version (must address): {feedback}\n" if feedback else "")
    )

    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]
    try:
        return chat_completion(
            messages=messages, api_key=api_key, base_url=base_url, model=model,
            temperature=0.6, max_tokens=700,
        ).strip()
    except Exception as exc:
        logger.error("generate_cover_letter_body LLM error: %s", exc)
        return (
            f"I am applying for the {job_title} position at {job_company}. "
            f"My background in {', '.join(skills_flat[:3])} aligns well with your requirements. "
            "I look forward to discussing how I can contribute to your team."
        )


def _parse_bullets_response(content: str) -> list[dict]:
    text = content.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text.strip())


_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers_in(text: str) -> set[str]:
    return {m.group().replace(",", "") for m in _NUM_RE.finditer(text or "")}


def _ground_tailored_bullets(original_entries: list[dict], tailored: list[dict]) -> list[dict]:
    """
    Anti-fabrication guard for rewritten bullets. Every number in a rewritten bullet
    must already exist somewhere in that entry's original bullets; a rewritten bullet
    that introduces a new metric is replaced with the original bullet at its position
    (or dropped if there is none). Entries for employers/titles not in the profile are
    dropped entirely.
    """
    orig_map = {
        (e.get("company") or "", e.get("title") or ""): e.get("bullets") or []
        for e in original_entries
    }
    grounded: list[dict] = []
    for entry in tailored if isinstance(tailored, list) else []:
        if not isinstance(entry, dict):
            continue
        key = (entry.get("company") or "", entry.get("title") or "")
        if key not in orig_map:
            logger.warning("tailor_resume_bullets: dropped invented entry %s", key)
            continue
        originals = orig_map[key]
        allowed_numbers: set[str] = set()
        for b in originals:
            allowed_numbers |= _numbers_in(b)
        bullets: list[str] = []
        for i, bullet in enumerate(entry.get("bullets") or []):
            if not isinstance(bullet, str) or not bullet.strip():
                continue
            if _numbers_in(bullet) - allowed_numbers:
                logger.warning(
                    "tailor_resume_bullets: reverted bullet with fabricated metric for %s", key
                )
                if i < len(originals):
                    bullets.append(originals[i])
                continue
            bullets.append(bullet)
        if bullets:
            grounded.append({"company": key[0], "title": key[1], "bullets": bullets})
    return grounded


def tailor_resume_bullets(
    profile_data: dict,
    job_title: str,
    job_description: str,
    api_key: str,
    base_url: str,
    model: str,
    insights: dict | None = None,
    feedback: str | None = None,
) -> list[dict]:
    experience = profile_data.get("experience", [])
    exp_json = [
        {"company": e.get("company"), "title": e.get("title") or e.get("role") or "", "bullets": e.get("bullets", [])}
        for e in experience
    ]
    keywords = (insights or {}).get("keywords") or []
    messages = [
        {
            "role": "system",
            "content": (
                "You are a resume writer tailoring experience bullets to a specific job. "
                "Rewrite each bullet so the most job-relevant part leads, keeping it truthful.\n"
                "Rules for every bullet:\n"
                "- Format: strong action verb + what was accomplished + quantified result. "
                "Keep every metric, number, and scale figure from the original EXACTLY as "
                "written — never invent, round, or estimate new numbers.\n"
                "- Where a bullet genuinely involves one of the job's keywords, use the "
                "keyword's exact spelling from the list so resume scanners match it. Do NOT "
                "claim technologies the original bullet doesn't support.\n"
                "- One line each: at most ~30 words. Cut filler, keep specifics.\n"
                "- Keep the same companies, titles, and bullet count; only reword and "
                "re-emphasize.\n"
                "Return a JSON array with the SAME structure: "
                '[{"company": str, "title": str, "bullets": [str, ...]}]. '
                "Return ONLY the JSON array."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Job title: {job_title}\nDescription:\n{job_description[:2500]}\n\n"
                + (f"Job keywords (use exact spelling where truthful): {', '.join(keywords)}\n\n" if keywords else "")
                + f"Experience entries:\n{json.dumps(exp_json, indent=2)}"
                + (f"\n\nUser feedback on the previous version (must address): {feedback}" if feedback else "")
            ),
        },
    ]
    try:
        raw = chat_completion(
            messages=messages, api_key=api_key, base_url=base_url, model=model,
            temperature=0.3, max_tokens=1200,
        )
        return _ground_tailored_bullets(exp_json, _parse_bullets_response(raw))
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

    # One analysis pass over the JD grounds everything downstream: ATS keywords,
    # ranked requirements, and company specifics for the cover letter.
    insights = extract_job_insights(
        profile_data, job.title, job.company, job.description or "", api_key, base_url, model
    )

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
        bullet_profile, job.title, job.description or "", api_key, base_url, model,
        insights=insights, feedback=feedback,
    )
    tailored_summary = tailor_summary(
        profile_data, job.title, job.company, insights, api_key, base_url, model,
        feedback=feedback,
    )
    cover_body = generate_cover_letter_body(
        profile_data, job.company, job.title, job.description or "", api_key, base_url, model,
        insights=insights, feedback=feedback,
        selected_experience=selected_experience, selected_projects=selected_projects,
    )

    # Resume
    resume_ctx = build_resume_context(
        profile_data,
        tailored_bullets if tailored_bullets else None,
        selected_experience=selected_experience,
        selected_projects=selected_projects,
        selected_skills=selected_skills,
        tailored_summary=tailored_summary,
    )

    # ATS second pass: when JD keywords are missing from the resume, first restore
    # any real profile skills that curation trimmed, then give the bullet rewriter
    # one retry targeting the keywords the candidate can truthfully claim.
    keywords = insights.get("keywords") or []
    present, missing = _keyword_coverage(resume_ctx, keywords)
    if missing:
        selected_skills = _restore_missing_skills(
            profile_data.get("skills") or {}, selected_skills, missing
        )
        resume_ctx = build_resume_context(
            profile_data,
            tailored_bullets if tailored_bullets else None,
            selected_experience=selected_experience,
            selected_projects=selected_projects,
            selected_skills=selected_skills,
            tailored_summary=tailored_summary,
        )
        present, missing = _keyword_coverage(resume_ctx, keywords)

        profile_terms_lower = {t.lower() for t in _profile_terms(profile_data)}
        retry_keywords = [k for k in missing if k.lower() in profile_terms_lower]
        if retry_keywords and tailored_bullets:
            retry_note = (
                "The previous version omitted these job keywords the candidate "
                f"genuinely has: {', '.join(retry_keywords)}. Weave each into a bullet "
                "where the original work truthfully involved it."
            )
            retry_feedback = f"{feedback}\n{retry_note}" if feedback else retry_note
            retried = tailor_resume_bullets(
                bullet_profile, job.title, job.description or "", api_key, base_url, model,
                insights=insights, feedback=retry_feedback,
            )
            if retried:
                tailored_bullets = retried
                resume_ctx = build_resume_context(
                    profile_data,
                    tailored_bullets,
                    selected_experience=selected_experience,
                    selected_projects=selected_projects,
                    selected_skills=selected_skills,
                    tailored_summary=tailored_summary,
                )
                present, missing = _keyword_coverage(resume_ctx, keywords)

    logger.info(
        "generate_documents %s: ATS keyword coverage %d/%d — missing: %s",
        application.id, len(present), len(present) + len(missing), ", ".join(missing) or "none",
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
