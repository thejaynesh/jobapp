"""
What document generation would actually get from the stored profile.

When a generated resume comes out empty there are three candidates: the profile
has nothing in it, the LLM tailoring returned nothing, or the LaTeX render
dropped it. They produce an identical artifact — a PDF with a header and
nothing under it — so guessing between them means changing code and hoping.

This settles the first one on its own. It reads the stored profile through the
exact functions generation uses (`build_resume_context`, then the resume
template) and reports what comes out. No LLM call, no network, no subprocess:
whatever this shows is what the profile contributes, independent of everything
downstream. If the content is here and the document is still blank, the profile
is not the problem and the search moves on.

The distinction that matters most is between missing and empty. An experience
list of three roles that all have zero bullets renders as three headings with
nothing beneath them — which reads on the page as "generation is broken" and in
the database as "the profile is fine".
"""

import logging

from app.services.doc_generator import (
    build_cover_letter_context,
    build_resume_context,
    render_latex,
)

logger = logging.getLogger(__name__)

# Sections the resume template will omit entirely when they are empty, in the
# order they appear on the page.
_RESUME_SECTIONS = ("Summary", "Skills", "Experience", "Projects", "Education")


def _entry_label(entry: dict) -> str:
    title = entry.get("title") or entry.get("role") or entry.get("name") or ""
    company = entry.get("company") or entry.get("school") or ""
    if title and company:
        return f"{title} — {company}"
    return title or company or "(untitled)"


def _bullet_count(entry: dict) -> int:
    return len([b for b in (entry.get("bullets") or []) if str(b).strip()])


def readiness(profile_data: dict) -> dict:
    """
    Section-by-section: what is there, what is thin, what would come out blank.

    `blockers` are the things that put a visibly empty document on the page.
    `warnings` are things worth fixing that still produce a document.
    """
    profile_data = profile_data or {}
    personal = profile_data.get("personal") or {}
    experience = profile_data.get("experience") or []
    projects = profile_data.get("projects") or []
    education = profile_data.get("education") or []
    skills = profile_data.get("skills") or {}
    summary = ((profile_data.get("narrative") or {}).get("summary") or "").strip()

    skill_categories = {k: v for k, v in skills.items() if v}
    skill_total = sum(len(v) for v in skill_categories.values())

    exp_rows = [
        {"label": _entry_label(e), "bullets": _bullet_count(e),
         "has_id": bool(e.get("id")), "tech": len(e.get("tech") or [])}
        for e in experience
    ]
    proj_rows = [
        {"label": _entry_label(p), "bullets": _bullet_count(p),
         "has_id": bool(p.get("id")), "tech": len(p.get("tech") or [])}
        for p in projects
    ]

    blockers: list[str] = []
    warnings: list[str] = []

    if not (personal.get("name") or "").strip():
        blockers.append("No name — the resume header renders blank.")
    if not (personal.get("email") or "").strip():
        warnings.append("No email, so the header has no way to contact you.")

    if not experience and not projects:
        blockers.append(
            "No experience and no projects — everything below the header is "
            "omitted, which is a one-line document."
        )
    elif exp_rows and all(row["bullets"] == 0 for row in exp_rows):
        # The case that reads as broken code and looks fine in the database.
        blockers.append(
            f"All {len(exp_rows)} experience entries have zero bullets, so the "
            "Experience section is headings with nothing underneath."
        )
    if projects and all(row["bullets"] == 0 for row in proj_rows):
        warnings.append(
            f"All {len(proj_rows)} projects have zero bullets — they render as "
            "titles with no content."
        )

    for row in exp_rows:
        if row["bullets"] == 0:
            warnings.append(f"Experience \"{row['label']}\" has no bullets.")
    for row in proj_rows:
        if row["bullets"] == 0:
            warnings.append(f"Project \"{row['label']}\" has no bullets.")

    if not skill_categories:
        blockers.append("No skills — the Skills section is omitted entirely.")
    if not education:
        warnings.append("No education entries, so that section is omitted.")
    if not summary:
        warnings.append(
            "No narrative summary saved. Generation usually writes a tailored one, "
            "but with the LLM unavailable there is nothing to fall back to."
        )

    # Selection asks the model to pick items by id. Without ids nothing it
    # returns can be matched, so curation silently degrades to "the first few".
    missing_ids = ([r["label"] for r in exp_rows if not r["has_id"]]
                   + [r["label"] for r in proj_rows if not r["has_id"]])
    if missing_ids:
        warnings.append(
            "These items have no id, so tailoring cannot select them by name and "
            f"falls back to profile order: {', '.join(missing_ids[:5])}"
        )

    return {
        "sections": [
            {"name": "Name and contact",
             "count": len([v for v in personal.values() if str(v or '').strip()]),
             "detail": (personal.get("name") or "—")},
            {"name": "Narrative summary", "count": 1 if summary else 0,
             "detail": (summary[:110] + "…") if len(summary) > 110 else (summary or "—")},
            {"name": "Skills", "count": skill_total,
             "detail": ", ".join(skill_categories.keys()) or "—"},
            {"name": "Experience", "count": len(exp_rows),
             "detail": f"{sum(r['bullets'] for r in exp_rows)} bullets in total"},
            {"name": "Projects", "count": len(proj_rows),
             "detail": f"{sum(r['bullets'] for r in proj_rows)} bullets in total"},
            {"name": "Education", "count": len(education),
             "detail": ", ".join(e.get("school", "") for e in education) or "—"},
        ],
        "experience": exp_rows,
        "projects": proj_rows,
        "blockers": blockers,
        "warnings": warnings,
        "ok": not blockers,
    }


def rendered(profile_data: dict) -> dict:
    """
    Put the profile through the real resume template, with no tailoring at all.

    This is the half of the question the counts cannot answer: content can be
    present in the profile and still not reach the page, because every section
    in the template is conditional. Which sections survive the render is the
    evidence — and because no LLM is involved, a section missing here is the
    profile's doing and nothing else's.
    """
    profile_data = profile_data or {}
    try:
        context = build_resume_context(profile_data, None)
        tex = render_latex("resume.tex.j2", context)
    except Exception as exc:
        logger.warning("profile_check: resume render failed: %s", exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "sections": [], "characters": 0, "cover_letter_ok": False}

    present = [name for name in _RESUME_SECTIONS if f"\\section{{{name}}}" in tex]
    body = tex.split(r"\begin{document}", 1)[-1]

    cover_ok = False
    cover_error = None
    try:
        # The cover letter shares the header context, so a profile problem that
        # blanks one usually blanks the other — worth checking in the same pass.
        render_latex("cover_letter.tex.j2", build_cover_letter_context(
            profile_data, "Example Co", "Software Engineer",
            "This is placeholder body text standing in for the generated letter.",
        ))
        cover_ok = True
    except Exception as exc:
        cover_error = f"{type(exc).__name__}: {exc}"
        logger.warning("profile_check: cover letter render failed: %s", exc)

    return {
        "ok": True,
        "error": None,
        "sections": [{"name": name, "present": name in present}
                     for name in _RESUME_SECTIONS],
        "characters": len(body.strip()),
        "cover_letter_ok": cover_ok,
        "cover_letter_error": cover_error,
    }


def report(profile_data: dict) -> dict:
    """Both halves, for the panel."""
    return {"readiness": readiness(profile_data), "rendered": rendered(profile_data)}
