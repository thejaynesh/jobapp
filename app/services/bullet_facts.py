"""
The numbers a bullet leaves out, asked for once and kept as facts.

"Built the ingestion service" and "Built the ingestion service that handles
2M events a day" are the same work. Only the second one gets a recruiter to
the next bullet. The generator cannot close that gap on its own: it may not
invent a figure (`_ground_tailored_bullets` reverts any bullet that adds a
number), so a bullet with no number in the profile gets none on any resume.

The only source for the number is the person, so the profile page asks. Each
bullet without a figure gets one question, chosen from what the bullet says it
did: "by how much?" for a reduction, "how many people?" for leading. The
answer is kept on the entry as a fact:

    entry["facts"] = [{"id", "about": <the bullet>, "answer": "2M events a day"}]

or, for a bullet where no number makes sense, `{"id", "about", "skipped":
True}`, so it is not asked again.

Facts are evidence, not bullets. The bullet rewriter gets an entry's facts
next to its bullets and may use them, and their figures pass the invented-
number guard. The cover letter, drafted answers and outreach messages list them
with the bullets. The profile's own bullets stay as the person wrote them.
"""

import copy
import re
import uuid

FACTS = "facts"
SECTIONS = ("experience", "projects")

_NUMBER_WORDS = re.compile(
    r"\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|dozens?|"
    r"hundreds?|thousands?|millions?|billions?|half|double[ds]?|tripled?)\b",
    re.IGNORECASE,
)

# The first pattern a bullet matches picks its question. Order matters:
# "reduced build time by automating" is a reduction before it is automation.
_ASKS = (
    (r"\b(reduc|cut|lower|decreas|shr[ai]nk|sav|trimm|eliminat)",
     "By how much? A percentage, or before and after."),
    (r"\b(improv|increas|boost|rais|grew|grow|sped|speed|faster|accelerat|optimi[sz]|scal)",
     "By how much? A percentage, or before and after."),
    (r"\b(led|lead|manag|mentor|supervis|coordinat|hir|onboard|train)",
     "How many people, and for how long?"),
    (r"\b(automat|streamlin|replac)",
     "How much time or manual work did it save, and how often?"),
    (r"\b(migrat|moved|port|upgrad)",
     "How much was moved (services, data, users), and what did it change?"),
    (r"\b(test|monitor|alert|debug|fix|resolv|triag)",
     "How many issues, or how much coverage or uptime?"),
    (r"\b(built|build|develop|creat|design|implement|launch|ship|wrote|writ|made)",
     "How many used it (people, requests, teams), or what did it change?"),
)
_DEFAULT_ASK = "What number shows its size or result? Users, time, money or volume."


def has_number(text: str) -> bool:
    return bool(re.search(r"\d", text or "")) or bool(_NUMBER_WORDS.search(text or ""))


def ask(bullet: str) -> str:
    lower = (bullet or "").lower()
    for pattern, question in _ASKS:
        if re.search(pattern, lower):
            return question
    return _DEFAULT_ASK


def answers(entry: dict) -> list[str]:
    """The entry's facts as the generator reads them."""
    return [f["answer"] for f in entry.get(FACTS) or []
            if isinstance(f, dict) and not f.get("skipped") and f.get("answer")]


def unanswered(entry: dict) -> list[dict]:
    """The entry's bullets with no figure that have not been answered or skipped."""
    settled = {f.get("about") for f in entry.get(FACTS) or [] if isinstance(f, dict)}
    return [{"bullet": b, "question": ask(b)} for b in entry.get("bullets") or []
            if b and not has_number(b) and b not in settled]


def count_unanswered(profile_data: dict) -> int:
    from app.services.profile_service import for_documents

    shown = for_documents(profile_data or {})
    return sum(len(unanswered(e)) for section in SECTIONS for e in shown.get(section) or [])


def _update(db, section: str, item_id: str, change) -> object:
    from app.services.profile_service import get_or_create_profile

    if section not in SECTIONS:
        raise KeyError(section)
    profile = get_or_create_profile(db)
    data = copy.deepcopy(profile.data or {})
    for entry in data.get(section) or []:
        if entry.get("id") == item_id:
            entry[FACTS] = change(list(entry.get(FACTS) or []))
            break
    else:
        raise KeyError(item_id)
    profile.data = data
    db.flush()
    return profile


def add(db, section: str, item_id: str, about: str, answer: str):
    """Keep `answer` as a fact about the bullet `about` (or the entry, when blank)."""
    answer = " ".join((answer or "").split())
    if not answer:
        return _update(db, section, item_id, lambda facts: facts)
    fact = {"id": uuid.uuid4().hex[:12], "about": about, "answer": answer}
    # A second answer to the same bullet replaces the first; facts about the
    # entry as a whole (no bullet) accumulate.
    return _update(db, section, item_id,
                   lambda facts: [f for f in facts if not about or f.get("about") != about] + [fact])


def skip(db, section: str, item_id: str, about: str):
    fact = {"id": uuid.uuid4().hex[:12], "about": about, "skipped": True}
    return _update(db, section, item_id,
                   lambda facts: [f for f in facts if f.get("about") != about] + [fact])


def remove(db, section: str, item_id: str, fact_id: str):
    return _update(db, section, item_id,
                   lambda facts: [f for f in facts if f.get("id") != fact_id])
