"""
What a generated resume or letter says that the profile does not.

The prompts ask for it — "do NOT claim technologies the original bullet doesn't
support", "never invent employers" — and a prompt is a request. The one rule
enforced in code was numbers in rewritten bullets
(`doc_generator._ground_tailored_bullets`). The rest of what a model can do to
a resume went unchecked:

* move a skill the candidate has onto a job where they did not use it. The ATS
  retry asks for exactly that — "weave each into a bullet where the original
  work truthfully involved it" — and has no way to know where that was;
* change an employer, a title or a date on the way through;
* put a figure or a years-of-experience claim in the summary or the letter,
  which no guard read at all.

These are string comparisons against the profile: cheap enough for every
generation, and exact about what they report — a term the posting or profile
knows as a skill, in text the profile has no source for. They do not rewrite
anything. The review panel lists them beside the text, where it can be edited
before it is sent.

A finding is a dict: `kind` (`tech`, `figure`, `years`, `identity`, `entry`),
`where` (the part of the document), `term`, `text` (the bullet or sentence it
is in) and `detail` (the sentence the page shows).
"""

import re
from contextlib import contextmanager
from contextvars import ContextVar

from app.services.experience import total_years

_YEARS_CLAIM = re.compile(r"(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)\b", re.IGNORECASE)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_FIRST_PERSON = re.compile(r"\b(i|i'm|i've|i'd|my|me)\b", re.IGNORECASE)
_CLAUSE = re.compile(r"[,;:()\u2014\u2013]|\s-\s|\b(?:which|because|while|whereas|where|but)\b",
                     re.IGNORECASE)
# "your" a few words before a term makes it the employer's.
_YOURS = re.compile(r"\b(?:your|you're|you've)\b(?:\W+\w+){0,3}\W*$", re.IGNORECASE)
# A claim may run this far past what the dates add up to before it is named:
# "2+ years" for 1.8 years of dated work is how people round.
YEARS_TOLERANCE = 0.5
# Skills that are also everyday words. "Helped the team go live" is not a
# claim to know Go, so in written text these count only when capitalised
# mid-sentence the way the skill is written.
_EVERYDAY = frozenset({
    "go", "rest", "node", "swift", "rust", "spark", "express", "chef", "puppet",
    "salt", "flask", "hive", "storm", "beam", "dart", "ember", "julia", "shell",
})
# A "keyword" this long is a requirement sentence, not a term to look for.
_MAX_TERM_WORDS = 3


# The skill names in force for one check: the built-in ones merged with the
# profile's own (Skills tab), set by check_resume and check_letter for their
# duration rather than passed through every helper.
_ALIASES: ContextVar[dict | None] = ContextVar("content_check_aliases", default=None)


@contextmanager
def _aliases_of(profile_data: dict):
    from app.services.matcher import alias_index

    token = _ALIASES.set(alias_index(profile_data))
    try:
        yield
    finally:
        _ALIASES.reset(token)


def _names(term: str) -> frozenset[str]:
    from app.services.matcher import _ALIAS_INDEX

    name = term.lower().strip()
    return (_ALIASES.get() or _ALIAS_INDEX).get(name, frozenset({name}))


def _pattern(name: str) -> re.Pattern:
    """How `name` is found in written text, as `matcher._mentions` finds it in a posting."""
    if name in _EVERYDAY:
        return re.compile(r"(?<=\S\s)" + re.escape(name.capitalize()) + r"\b")
    if " " in name:
        return re.compile(re.escape(name), re.IGNORECASE)
    if re.fullmatch(r"\w+", name):
        return re.compile(r"\b" + re.escape(name) + r"\b", re.IGNORECASE)
    return re.compile(r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])", re.IGNORECASE)


def _where(text: str, term: str) -> list[int]:
    return [m.start() for name in _names(term) for m in _pattern(name).finditer(text)]


def says(text: str, term: str) -> bool:
    """Whether written text (as written, not lowercased) uses `term`."""
    return bool(_where(text, term))


def _claims(sentence: str, term: str) -> bool:
    """
    Whether a letter's sentence says the candidate has `term`: it is in a
    clause about the candidate, and not one of the employer's ("your Kafka
    platform", "the Kafka work your team does").
    """
    for clause in _CLAUSE.split(sentence):
        if not clause or not _FIRST_PERSON.search(clause):
            continue
        if any(not _YOURS.search(clause[:at]) for at in _where(clause, term)):
            return True
    return False


def _has(source_lower: str, term: str) -> bool:
    """Whether the profile's text has `term` in any spelling — generous on purpose."""
    from app.services.matcher import _mentions

    return any(_mentions(source_lower, name) for name in _names(term))


def _numbers(text: str) -> set[str]:
    from app.services.doc_generator import _numbers_in

    return _numbers_in(text)


def _strings(value) -> list[str]:
    """Every string in a nested profile value."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _strings(v)]
    return []


def vocabulary(profile_data: dict, keywords=(), job=None) -> list[str]:
    """
    The terms a check looks for: the posting's keywords and stated skills —
    what a model is pushed to put in — and every skill the profile lists,
    which is what it is tempted to move from one job to another.
    """
    from app.services.doc_generator import _profile_terms

    terms = list(keywords or [])
    if job is not None:
        terms += list(job.required_skills or []) + list(job.nice_to_have_skills or [])
    terms += sorted(_profile_terms(profile_data))
    seen, found = set(), []
    for term in terms:
        term = (term or "").strip()
        key = term.lower()
        # One letter ("C", "R") matches every initial and list marker.
        if len(key) < 2 or len(key.split()) > _MAX_TERM_WORDS or key in seen:
            continue
        seen.add(key)
        found.append(term)
    return found


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.split(text or "") if s.strip()]


def _label(entry: dict) -> str:
    return (entry.get("company") or entry.get("name") or entry.get("school")
            or entry.get("title") or "an entry")


def _profile_entry(entry: dict, originals: list[dict]) -> dict | None:
    if entry.get("id"):
        for original in originals:
            if original.get("id") == entry["id"]:
                return original
    title = (entry.get("title") or entry.get("role") or "").strip().lower()
    name = (entry.get("company") or entry.get("name") or entry.get("school") or "").strip().lower()
    for original in originals:
        other = (original.get("company") or original.get("name") or original.get("school") or "")
        other_title = original.get("title") or original.get("role") or ""
        if other.strip().lower() == name and other_title.strip().lower() == title:
            return original
    return None


_IDENTITY = {
    "experience": (("company", "company"), ("title", "title"), ("start_date", "start date"),
                   ("end_date", "end date")),
    "projects": (("name", "name"),),
    "education": (("school", "school"), ("degree", "degree"),
                  ("start_date", "start date"), ("end_date", "end date")),
}


def _field(entry: dict, key: str) -> str:
    value = entry.get(key)
    if key == "title" and not value:
        value = entry.get("role")
    return " ".join(str(value or "").split())


def _identity(section: str, entry: dict, original: dict) -> list[dict]:
    found = []
    for key, name in _IDENTITY[section]:
        written, source = _field(entry, key), _field(original, key)
        if written != source:
            found.append({
                # The detail quotes both values; there is no sentence to point at.
                "kind": "identity", "where": section, "term": name, "text": "",
                "detail": (f"The {_label(original)} entry's {name} reads "
                           f"“{written or 'nothing'}”; your profile says "
                           f"“{source or 'nothing'}”."),
            })
    return found


def _bullets(section: str, entry: dict, original: dict, terms: list[str]) -> list[dict]:
    source = " ".join(_strings({k: v for k, v in original.items() if k != "id"})).lower()
    source_numbers = _numbers(source)
    found = []
    for bullet in entry.get("bullets") or []:
        if bullet.lower() in source:
            continue  # the profile's own bullet, word for word
        for term in terms:
            if says(bullet, term) and not _has(source, term):
                found.append({
                    "kind": "tech", "where": section, "term": term, "text": bullet,
                    "detail": (f"{term} is in a bullet under {_label(original)} but nowhere "
                               "in that entry in your profile."),
                })
        for figure in sorted(_numbers(bullet) - source_numbers):
            found.append({
                "kind": "figure", "where": section, "term": figure, "text": bullet,
                "detail": (f"{figure} is in a bullet under {_label(original)} but nowhere "
                           "in that entry in your profile."),
            })
    return found


def _prose(where: str, text: str, profile_data: dict, terms: list[str],
           *, claims_only: bool, other_sources: str = "") -> list[dict]:
    """The summary or the letter, against everything the profile says."""
    profile_text = " ".join(_strings(profile_data)).lower()
    known_numbers = _numbers(profile_text) | _numbers(other_sources)
    worked = total_years(profile_data.get("experience") or [])
    found = []
    for sentence in _sentences(text):
        years = list(_YEARS_CLAIM.finditer(sentence))
        for match in years:
            if (float(match.group(1)) > worked + YEARS_TOLERANCE
                    and match.group(0).lower() not in profile_text):
                found.append({
                    "kind": "years", "where": where, "term": match.group(0), "text": sentence,
                    "detail": (f"The {where} says “{match.group(0)}”; the dates on "
                               f"your experience add up to {worked:g} years."),
                })
        # A letter names the employer's stack ("your Kafka platform"); only a
        # clause about the candidate claims to have used it.
        for term in terms:
            claimed_here = _claims(sentence, term) if claims_only else says(sentence, term)
            if claimed_here and not _has(profile_text, term):
                found.append({
                    "kind": "tech", "where": where, "term": term, "text": sentence,
                    "detail": f"The {where} claims {term}; your profile never mentions it.",
                })
        claimed = {m.group(1) for m in years}
        for figure in sorted(_numbers(sentence) - known_numbers - claimed):
            found.append({
                "kind": "figure", "where": where, "term": figure, "text": sentence,
                "detail": f"{figure} in the {where} is not a figure from your profile.",
            })
    return found


def check_resume(ctx: dict, profile_data: dict, keywords=(), job=None) -> list[dict]:
    """Everything in a resume context the profile does not support."""
    with _aliases_of(profile_data):
        return _check_resume(ctx, profile_data, keywords, job)


def check_letter(body: str, profile_data: dict, keywords=(), job=None) -> list[dict]:
    """Figures, years and first-person skill claims in a letter the profile does not support."""
    posting = " ".join(str(x) for x in (getattr(job, "description", None),
                                        getattr(job, "title", None),
                                        getattr(job, "company", None)) if x)
    with _aliases_of(profile_data):
        return _prose("letter", body or "", profile_data, vocabulary(profile_data, keywords, job),
                      claims_only=True, other_sources=posting)


def _check_resume(ctx: dict, profile_data: dict, keywords, job) -> list[dict]:
    terms = vocabulary(profile_data, keywords, job)
    found: list[dict] = []
    for section in ("experience", "projects", "education"):
        originals = profile_data.get(section) or []
        for entry in ctx.get(section) or []:
            original = _profile_entry(entry, originals)
            if original is None:
                found.append({
                    "kind": "entry", "where": section, "term": _label(entry), "text": "",
                    "detail": f"{_label(entry)} is on the resume but not in your profile.",
                })
                continue
            found += _identity(section, entry, original)
            if section != "education":
                found += _bullets(section, entry, original, terms)
    found += _prose("summary", ctx.get("narrative_summary") or "", profile_data, terms,
                    claims_only=False)
    return found


def carried_over(findings: list[dict], earlier: list[dict]) -> list[dict]:
    """
    After a hand edit, the findings the edit left standing.

    A finding survives only where the earlier version had the same one on the
    same text — the model's words, kept. What the user typed or changed is
    theirs to vouch for: naming their own figure as "not from your profile" on
    every save would teach them to stop reading the list. An employer, title
    or date that differs from the profile is named whoever wrote it, since the
    profile is where those are kept.
    """
    before = {(f.get("kind"), f.get("term"), f.get("text")) for f in earlier or []}
    return [f for f in findings
            if f["kind"] in ("identity", "entry") or (f["kind"], f["term"], f["text"]) in before]
