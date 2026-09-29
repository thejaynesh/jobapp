"""
Editing a generated document by hand, as a new version.

A generated resume used to be a PDF to take or regenerate. Regenerating spends
six model calls to change one bullet, and cannot keep the edits you would have
made yourself. And editing is worth doing for its own sake: on Freelancer,
the time applicants spent editing an AI-drafted cover letter went with
getting hired, while letters left as drafted lost about half their value as a
signal (Cui, Dias & Ye, 2025).

An edit takes the version's stored content (`ApplicationDocument.content`),
applies the form, recompiles through the same template and one-page fit, runs
the same keyword check on the new PDF, and saves it as the next version —
current, with the earlier ones still in the history.
"""

import copy
import uuid

from app.models.application import ApplicationDocument, DocType

EDITED_BY = "you (edited)"


class NotEditable(Exception):
    """The version has no stored content to edit (written before 0047)."""


class StaleEdit(NotEditable):
    """The base changed while the user edited or while the PDF was compiled."""


def _assert_current(db, application, previous, *, lock=False):
    from app.models.application import Application
    if lock:
        db.query(Application.id).filter(Application.id == application.id).with_for_update().one()
    current = db.query(ApplicationDocument.id).filter(
        ApplicationDocument.application_id == application.id,
        ApplicationDocument.doc_type == previous.doc_type, ApplicationDocument.is_current.is_(True)).scalar()
    if current != previous.id:
        raise StaleEdit("A newer document is current. Reload the application and apply your changes to that version.")


def _lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _skills_from_text(text: str) -> dict:
    """"Category: a, b" per line back into {category: [a, b]}."""
    skills: dict = {}
    for line in _lines(text):
        category, _, items = line.partition(":")
        if not items:
            category, items = "Skills", category
        values = [item.strip() for item in items.split(",") if item.strip()]
        if values:
            skills.setdefault(category.strip() or "Skills", []).extend(values)
    return skills


def skills_as_text(skills: dict) -> str:
    return "\n".join(f"{category}: {', '.join(items)}"
                     for category, items in (skills or {}).items() if items)


def edited_resume_context(content: dict, form) -> dict:
    """The stored context with the form's summary, bullets and skills applied."""
    from app.services.doc_generator import _ordered_skills

    ctx = copy.deepcopy(content.get("context") or {})
    if "summary" in form:
        ctx["narrative_summary"] = " ".join(str(form.get("summary") or "").split())
    for key in ("experience", "projects"):
        for index, entry in enumerate(ctx.get(key) or []):
            field = f"{key}-{index}-bullets"
            if field in form:
                entry["bullets"] = _lines(form.get(field))
    if "skills" in form:
        ctx["skills"] = _skills_from_text(form.get("skills"))
        ctx["skills_ordered"] = _ordered_skills(ctx["skills"])
    return ctx


def _save(db, application, previous: ApplicationDocument, doc_type: DocType,
          path, content: dict) -> ApplicationDocument:
    from app.services.doc_generator import _next_version, _set_only_current
    _assert_current(db, application, previous, lock=True)
    version = _next_version(db, application.id, doc_type)
    doc = ApplicationDocument(
        application_id=application.id, doc_type=doc_type, version=version,
        path=str(path), generation_feedback=f"Edited by hand from v{previous.version}",
        generated_by=EDITED_BY, content=content,
    )
    _set_only_current(db, application.id, doc_type, doc)
    db.add(doc)
    db.commit()
    return doc


def _output_path(application, doc_type: DocType, version: int):
    from app.services.doc_generator import _OUTPUT_DIR

    name = "resume" if doc_type == DocType.resume else "cover_letter"
    # Two compilations starting from the same version must never overwrite
    # one another's file, even when one is rejected by the final version check.
    return _OUTPUT_DIR / str(application.id) / f"{application.id}_{name}_v{version}_{uuid.uuid4().hex[:12]}.pdf"


def _profile_for_documents(db) -> dict:
    from app.models.profile import Profile
    from app.services.profile_service import for_documents

    profile = db.query(Profile).first()
    return for_documents(profile.data if profile else {})


def save_resume(db, application, previous: ApplicationDocument, form) -> ApplicationDocument:
    """Apply the form to `previous` and save the result as the current resume."""
    from app.services import content_checks, document_content
    from app.services.doc_generator import _next_version, compile_resume_one_page

    content = previous.content or {}
    if content.get("kind") != "resume" or not content.get("context"):
        raise NotEditable("This version was written before edits were possible; "
                          "regenerate once to edit it.")
    _assert_current(db, application, previous)
    ctx = edited_resume_context(content, form)
    path = _output_path(application, DocType.resume,
                        _next_version(db, application.id, DocType.resume))
    from app.services.matcher import alias_index

    keywords = (content.get("ats") or {}).get("keywords") or []
    profile_data = _profile_for_documents(db)
    checks = content_checks.check_resume(ctx, profile_data, keywords, application.job)
    from app.services import document_evidence
    from types import SimpleNamespace
    job = SimpleNamespace(**{key: getattr(application.job, key, None) for key in
        ("description", "required_skills", "nice_to_have_skills", "required_years", "education_required", "location", "match_assessment")})
    db.commit()
    compiled = compile_resume_one_page(ctx, path)
    new_content = {
        **content,
        "context": ctx,
        "ats": document_content.ats_check(compiled, keywords, ctx, alias_index(profile_data)),
        "checks": content_checks.carried_over(checks, content.get("checks")),
        "edited_from": previous.version,
        "evidence": document_evidence.manifest(job, profile_data, compiled, ctx),
        "diff": document_evidence.diff(content.get("context") or {}, ctx),
    }
    return _save(db, application, previous, DocType.resume, compiled, new_content)


_KEEP = object()


def save_letter(db, application, previous: ApplicationDocument, body: str,
                recipient=_KEEP) -> ApplicationDocument:
    """
    Save an edited letter body as the current cover letter.

    `recipient` is a letter_recipient dict, None for "Dear Hiring Manager",
    or left out to keep whoever the letter was addressed to.
    """
    from app.services import content_checks
    from app.services.doc_generator import _next_version, compile_pdf, render_latex

    content = previous.content or {}
    if content.get("kind") != "cover_letter" or not content.get("context"):
        raise NotEditable("This version was written before edits were possible; "
                          "regenerate once to edit it.")
    _assert_current(db, application, previous)
    ctx = {**content["context"], "cover_letter_body": (body or "").strip()}
    if recipient is not _KEEP:
        ctx["recipient"] = recipient
    path = _output_path(application, DocType.cover_letter,
                        _next_version(db, application.id, DocType.cover_letter))
    checks = content_checks.check_letter(ctx["cover_letter_body"], _profile_for_documents(db),
                                         content.get("keywords") or [], application.job)
    db.commit()
    compiled = compile_pdf(render_latex("cover_letter.tex.j2", ctx), path)
    return _save(db, application, previous, DocType.cover_letter, compiled, {
        **content, "context": ctx, "edited_from": previous.version,
        "checks": content_checks.carried_over(checks, content.get("checks")),
    })


def review(content: dict | None) -> dict | None:
    """What the page shows about a resume version: each entry with its changes marked."""
    if not content or content.get("kind") != "resume":
        return None
    originals = content.get("original_bullets") or {}
    ctx = content.get("context") or {}
    sections = []
    for key, label in (("experience", "Experience"), ("projects", "Projects")):
        entries = []
        for index, entry in enumerate(ctx.get(key) or []):
            original = originals.get(entry.get("id") or "", None)
            bullets = entry.get("bullets") or []
            entries.append({
                "field": f"{key}-{index}-bullets",
                "heading": " — ".join(x for x in (entry.get("title") or entry.get("name"),
                                                  entry.get("company")) if x),
                "bullets": bullets,
                "original": original,
                # A bullet not word for word in the profile was written or
                # rewritten for this job.
                "rewritten": [b for b in bullets if original is not None and b not in original],
            })
        sections.append({"key": key, "label": label, "entries": entries})
    return {
        "summary": ctx.get("narrative_summary") or "",
        "skills": skills_as_text(ctx.get("skills") or {}),
        "sections": sections,
        "ats": content.get("ats") or {},
        "checks": content.get("checks") or [],
    }
