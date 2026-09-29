"""
Other ways to say what an entry did, kept beside it.

A profile entry has one set of bullets, and every generation rewrites them for
the job at hand, then forgets the rewrite. Some of those rewrites are better
than the originals: a sharper verb, the metric moved to the front, the
posting's term for what you did. They were lost the moment the PDF was made.

So each entry has a bank (`entry["bank"]`) of alternative bullets:

* **offered**: a bullet a generation wrote for this entry that is not in the
  profile word for word and that the invented-content check had nothing to say
  about, with the job it was written for. Offered, not added: it is the
  model's wording until you keep it;
* **kept**: one you kept, or wrote yourself. Kept bullets go to the bullet
  rewriter as approved phrasings it may use in place of a bullet, and their
  figures pass its invented-number guard;
* **dismissed**: remembered only so the same wording is not offered again.

"Use in resumes" moves a kept bullet into the entry's own bullets.
"""

import copy
import uuid
from datetime import datetime, timezone

BANK = "bank"
SECTIONS = ("experience", "projects")
# Offers pile up one generation at a time; the oldest go first.
MAX_OFFERED = 20
MAX_DISMISSED = 100


def _norm(text: str) -> str:
    return " ".join((text or "").split()).lower()


def items(entry: dict, status: str | None = None) -> list[dict]:
    return [b for b in entry.get(BANK) or []
            if isinstance(b, dict) and (status is None or b.get("status") == status)]


def kept(entry: dict) -> list[str]:
    return [b["text"] for b in items(entry, "kept") if b.get("text")]


def offer(profile_data: dict, section: str, entry_id: str, texts: list[str],
          job_label: str) -> int:
    """Offer these generated bullets to the entry's bank, in place. Returns how many were new."""
    entry = next((e for e in profile_data.get(section) or [] if e.get("id") == entry_id), None)
    if entry is None:
        return 0
    seen = {_norm(b) for b in entry.get("bullets") or []}
    seen |= {_norm(b.get("text")) for b in items(entry)}
    bank = items(entry)
    added = 0
    for text in texts:
        text = " ".join((text or "").split())
        if not text or _norm(text) in seen:
            continue
        seen.add(_norm(text))
        bank.append({"id": uuid.uuid4().hex[:12], "text": text, "status": "offered",
                     "source": "generated", "job": job_label,
                     "at": datetime.now(timezone.utc).isoformat()})
        added += 1
    offered = [b for b in bank if b["status"] == "offered"]
    dismissed = [b for b in bank if b["status"] == "dismissed"]
    drop = {b["id"] for b in offered[:-MAX_OFFERED]} | {b["id"] for b in dismissed[:-MAX_DISMISSED]}
    entry[BANK] = [b for b in bank if b["id"] not in drop]
    return added


def offer_from_generation(profile_data: dict, resume_ctx: dict, checks: list[dict],
                          job_label: str) -> int:
    """Every rewritten, unflagged bullet of a generated resume, offered to its entry."""
    flagged = {f.get("text") for f in checks or [] if f.get("text")}
    total = 0
    for section in SECTIONS:
        for entry in resume_ctx.get(section) or []:
            if entry.get("id"):
                texts = [b for b in entry.get("bullets") or [] if b not in flagged]
                total += offer(profile_data, section, entry["id"], texts, job_label)
    return total


def _change(db, section: str, entry_id: str, change):
    from app.services.profile_service import get_or_create_profile

    if section not in SECTIONS:
        raise KeyError(section)
    profile = get_or_create_profile(db)
    data = copy.deepcopy(profile.data or {})
    entry = next((e for e in data.get(section) or [] if e.get("id") == entry_id), None)
    if entry is None:
        raise KeyError(entry_id)
    change(entry)
    profile.data = data
    db.flush()
    return profile


def _set_status(entry: dict, item_id: str, status: str) -> None:
    for b in items(entry):
        if b["id"] == item_id:
            b["status"] = status


def keep(db, section, entry_id, item_id):
    return _change(db, section, entry_id, lambda e: _set_status(e, item_id, "kept"))


def dismiss(db, section, entry_id, item_id):
    return _change(db, section, entry_id, lambda e: _set_status(e, item_id, "dismissed"))


def add_own(db, section, entry_id, text):
    text = " ".join((text or "").split())

    def change(entry):
        if text and _norm(text) not in {_norm(b.get("text")) for b in items(entry)}:
            entry[BANK] = items(entry) + [{"id": uuid.uuid4().hex[:12], "text": text,
                                           "status": "kept", "source": "you", "job": "",
                                           "at": datetime.now(timezone.utc).isoformat()}]
    return _change(db, section, entry_id, change)


def use(db, section, entry_id, item_id):
    """Move a kept bullet into the entry's own bullets."""
    def change(entry):
        chosen = next((b for b in items(entry) if b["id"] == item_id), None)
        if chosen is None:
            return
        entry["bullets"] = list(entry.get("bullets") or []) + [chosen["text"]]
        entry[BANK] = [b for b in items(entry) if b["id"] != item_id]
    return _change(db, section, entry_id, change)


def remove(db, section, entry_id, item_id):
    def change(entry):
        entry[BANK] = [b for b in items(entry) if b["id"] != item_id]
    return _change(db, section, entry_id, change)
