"""
What a generated document says, kept with the file (`ApplicationDocument.content`).

A generation used to end at a PDF path. The tailored summary, the rewritten
bullets, the letter and every check made on them lived for one call, so the
application page could offer a download and nothing else, and the one check
worth reading — which of the posting's keywords made it in — sat in the worker
log. Kept here, per version, they can be shown, edited and re-rendered.
"""

from app.services import pdf_text


def ats_check(pdf_path, keywords: list[str], resume_ctx: dict) -> dict:
    """
    Which of the posting's keywords a parser finds in the compiled resume.

    Read from the PDF itself, not from the template's input: the one-page trim
    can cut a bullet after the context was built, and a ligature can hide a
    word that is on the page. Falls back to the context only when the file
    yields no text at all, and says so.
    """
    text = pdf_text.extract(pdf_path)
    if text.strip():
        present, missing = pdf_text.coverage(text, keywords)
        source = "pdf"
    else:
        from app.services.doc_generator import _keyword_coverage

        present, missing = _keyword_coverage(resume_ctx, keywords)
        source = "template"
    return {
        "keywords": list(keywords),
        "present": present,
        "missing": missing,
        "read_from": source,
        "ligatures": pdf_text.ligatures(text),
        "garbled": pdf_text.garbled(text),
        "chars": len(text),
    }


def original_bullets(profile_data: dict) -> dict:
    """Each profile entry's own bullets by id, to show what tailoring changed."""
    found = {}
    for section in ("experience", "projects"):
        for entry in profile_data.get(section) or []:
            if entry.get("id"):
                found[entry["id"]] = list(entry.get("bullets") or [])
    return found


def resume(resume_ctx: dict, ats: dict, profile_data: dict, **extra) -> dict:
    return {"kind": "resume", "context": resume_ctx, "ats": ats,
            "original_bullets": original_bullets(profile_data), **extra}


def cover_letter(letter_ctx: dict, **extra) -> dict:
    return {"kind": "cover_letter", "context": letter_ctx, **extra}
