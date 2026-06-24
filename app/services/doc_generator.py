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


def latex_escape(text) -> str:
    if not isinstance(text, str):
        return ""
    return _LATEX_SPECIAL.sub(lambda m: _LATEX_MAP[m.group()], text)


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

def build_resume_context(profile_data: dict, tailored_bullets: list[dict] | None) -> dict:
    experience = [dict(exp) for exp in profile_data.get("experience", [])]
    if tailored_bullets:
        bullet_map = {(e["company"], e["title"]): e["bullets"] for e in tailored_bullets}
        for exp in experience:
            key = (exp.get("company", ""), exp.get("title", ""))
            if key in bullet_map:
                exp["bullets"] = bullet_map[key]
    return {
        "profile": profile_data,
        "narrative_summary": profile_data.get("narrative", {}).get("summary", ""),
        "skills": profile_data.get("skills", {}),
        "experience": experience,
        "education": profile_data.get("education", []),
        "projects": profile_data.get("projects", []),
    }


def build_cover_letter_context(profile_data: dict, job_company: str, job_title: str, body: str) -> dict:
    return {
        "profile": profile_data,
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


def compile_pdf(tex_source: str, output_path: Path) -> Path:
    with tempfile.TemporaryDirectory() as tmpdir:
        tex_file = Path(tmpdir) / "document.tex"
        tex_file.write_text(tex_source, encoding="utf-8")
        result = subprocess.run(
            ["pdflatex", "-interaction=nonstopmode", "-output-directory", tmpdir, str(tex_file)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise DocGenerationError(
                f"pdflatex failed (exit {result.returncode}): {result.stderr[-500:]}"
            )
        compiled = Path(tmpdir) / "document.pdf"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(str(compiled), str(output_path))
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
    summary = profile_data.get("narrative", {}).get("summary", "")
    name = profile_data.get("name", "Candidate")
    experience = profile_data.get("experience", [])
    exp_summary = "; ".join(f"{e.get('title')} at {e.get('company')}" for e in experience[:3])

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
        {"company": e.get("company"), "title": e.get("title"), "bullets": e.get("bullets", [])}
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

    tailored_bullets = tailor_resume_bullets(
        profile_data, job.title, job.description or "", api_key, base_url, model
    )
    cover_body = generate_cover_letter_body(
        profile_data, job.company, job.title, job.description or "", api_key, base_url, model
    )

    # Resume
    resume_ctx = build_resume_context(profile_data, tailored_bullets if tailored_bullets else None)
    resume_tex = render_latex("resume.tex.j2", resume_ctx)
    resume_version = _next_version(db, application.id, DocType.resume)
    resume_filename = f"{application.id}_resume_v{resume_version}.pdf"
    resume_path = _OUTPUT_DIR / str(application.id) / resume_filename
    compiled_resume = compile_pdf(resume_tex, resume_path)

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
