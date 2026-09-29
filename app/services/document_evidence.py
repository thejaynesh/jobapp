"""Select approved achievements within each entry; never move claims between jobs."""
import copy
import difflib

from app.services import evidence, pdf_text
from app.services.tunables import value


def curate(entries, job, profile):
    from app.services.bullet_bank import kept
    targets = evidence.words((job.description or "")[:16000])
    limit = int(value(profile, "document_bullets_per_entry"))
    selected = []
    for entry in entries:
        item = copy.deepcopy(entry)
        candidates = list(dict.fromkeys([str(b) for b in (entry.get("bullets") or []) + kept(entry) if b]))
        chosen, covered = [], set()
        while candidates and len(chosen) < limit:
            def utility(bullet):
                tokens = evidence.words(bullet)
                gain = len((tokens & targets) - covered)
                redundancy = max((len(tokens & evidence.words(old)) / max(1, len(tokens | evidence.words(old))) for old in chosen), default=0)
                return gain - 4 * redundancy, -len(bullet)
            best = max(candidates, key=utility)
            candidates.remove(best)
            if any(evidence.normal(best) == evidence.normal(old) for old in chosen):
                continue
            chosen.append(best)
            covered.update(evidence.words(best) & targets)
        item["bullets"] = chosen
        selected.append(item)
    return selected


def manifest(job, profile, path, context):
    text = pdf_text.extract(path)
    facts = evidence.facts(profile)
    # The basis is scoped to the original employer/project. Rewritten text is
    # displayed for review, never asserted to be proven merely by similarity.
    links = []
    for section in ("experience", "projects"):
        for entry in context.get(section) or []:
            basis = [f for f in facts if f["source"] == section and f["entry_id"] == str(entry.get("id"))]
            for bullet in entry.get("bullets") or []:
                ranked = sorted(basis, key=lambda f: -len(evidence.words(f["text"]) & evidence.words(bullet)))[:2]
                links.append({"entry_id": entry.get("id"), "text": bullet, "basis": ranked,
                    "verbatim": any(evidence.normal(f["text"]) == evidence.normal(bullet) for f in ranked),
                    "readable": evidence.normal(bullet) in evidence.normal(text) if text else False})
    return {"version": 1, "coverage": evidence.coverage(job, profile, text),
            "profile_hash": evidence.fingerprint(facts), "posting_hash": evidence.posting_hash(job),
            "links": links, "rendered_text": text[:50000]}


def context_text(context):
    pieces = [context.get("narrative_summary") or context.get("cover_letter_body") or ""]
    for section in ("experience", "projects"):
        for entry in context.get(section) or []:
            pieces.append(" — ".join(str(entry.get(k) or "") for k in ("title", "name", "company")).strip(" —"))
            pieces.extend(entry.get("bullets") or [])
    for category, skills in (context.get("skills") or {}).items():
        pieces.append(category + ": " + ", ".join(skills))
    return "\n".join(pieces)


def diff(before, after):
    return "\n".join(difflib.unified_diff(context_text(before).splitlines(), context_text(after).splitlines(),
                                          fromfile="Base version", tofile="Proposed version", lineterm=""))


def interview(application, profile):
    document = next((d for d in application.documents if d.id == application.sent_resume_id), None)
    if not document:
        return {"version": None, "questions": [], "note": "Confirm the resume you submitted to prepare against its actual claims."}
    content = document.content or {}
    context = content.get("context") or {}
    assessment = evidence.current(application.job, profile)
    questions = []
    for section in ("experience", "projects"):
        for entry in context.get(section) or []:
            for bullet in entry.get("bullets") or []:
                relevance = len(evidence.words(bullet) & evidence.words(application.job.description))
                questions.append({"quote": bullet, "question": "What was your contribution, how did you measure the result, and what tradeoffs did you make?", "relevance": relevance})
    questions.sort(key=lambda q: -q["relevance"])
    return {"version": document.version, "document_id": str(document.id), "questions": questions[:6],
            "gaps": [r for r in assessment["requirements"] if r["status"] in {"unknown", "transferable"}][:5],
            "note": "These prompts quote the linked resume. Verify the submitted version below; inferred links are not proof of upload."}
