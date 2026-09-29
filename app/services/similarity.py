"""
How much a posting reads like the profile, for free.

Every job that clears the keyword filter goes to the model for a score, and
that call is what the match budget rations. The keyword filter asks only
whether enough of your listed skills are mentioned somewhere in the posting;
it cannot tell a backend posting that names Python once in a list of twenty
nice-to-haves from one that is about the work in your bullets.

This is the cheap middle step: TF-IDF cosine between the profile — target
roles, summary, bullets, skills — and the posting's title and description.
Rare words that both share count for a lot; words every posting uses
("experience", "team") count for almost nothing because their document
frequency, measured on the postings the tracker actually holds, says so.

It is stored on every scored job (`Job.similarity`, 0–100) and **decides
nothing by default**. `prescreen_min_similarity` is 0 until the matching report
has shown, on your own decisions, what a threshold would have cost: how many
model calls it saves against how many of the jobs you applied to it would
have dropped. Turning it on is then a measured choice, not a guess.
"""

import math
import re
import threading
import time
from collections import Counter

_WORD = re.compile(r"[a-z][a-z0-9+#.]*[a-z0-9+#]|[a-z]")
# Words every posting and every resume uses. The document frequency takes most
# of these down on its own; listing them keeps a small corpus from being
# fooled by them.
_STOP = frozenset("""
a about above after all also an and any are as at be been being both but by can could
do does each for from had has have having he her here hers him his how i if in into is
it its just me more most my no not of on once only or other our out over own same she
should so some such than that the their them then there these they this those through
to too under until up very was we were what when where which while who whom why will
with would you your yours ours us per via etc e.g i.e including include includes
experience experienced work working team teams role roles job jobs company candidate
candidates ability able strong excellent skills skill years year plus preferred required
requirements responsibilities responsible opportunity opportunities benefits apply
""".split())

# The document frequency is measured on this many recent postings and kept for
# this long: it moves with the market, not with each fetch.
CORPUS_SIZE = 2000
CORPUS_TTL_SECONDS = 6 * 3600

_cache: dict = {}
_lock = threading.Lock()


def tokens(text: str) -> list[str]:
    return [w.strip(".") for w in _WORD.findall((text or "").lower())
            if w not in _STOP and len(w.strip(".")) > 1]


def profile_text(profile_data: dict) -> str:
    """What the profile says about the work, weighted by repetition."""
    parts: list[str] = []
    roles = profile_data.get("target_roles") or []
    # Target roles are what the search is for; say them three times.
    parts += [" ".join(roles)] * 3
    parts.append((profile_data.get("narrative") or {}).get("summary") or "")
    for items in (profile_data.get("skills") or {}).values():
        parts.append(" ".join(items or []))
    for section in ("experience", "projects"):
        for entry in profile_data.get(section) or []:
            parts.append(" ".join(str(x) for x in (
                entry.get("role") or entry.get("title") or "", entry.get("name") or "",
                entry.get("description") or "", " ".join(entry.get("tech") or []),
                " ".join(entry.get("bullets") or []),
            )))
    return "\n".join(parts)


def job_text(job) -> str:
    title = getattr(job, "title", "") or ""
    skills = " ".join((getattr(job, "required_skills", None) or [])
                      + (getattr(job, "nice_to_have_skills", None) or []))
    # The title is a better summary of a posting than any paragraph of it.
    return f"{title} {title} {title}\n{skills}\n{getattr(job, 'description', '') or ''}"


def document_frequencies(db) -> tuple[dict, int]:
    """How many of the recent postings use each word, cached for a while."""
    with _lock:
        cached = _cache.get("df")
        if cached and time.monotonic() - cached[2] < CORPUS_TTL_SECONDS:
            return cached[0], cached[1]
    from app.models.job import Job

    rows = (db.query(Job.title, Job.description)
            .filter(Job.description.isnot(None))
            .order_by(Job.fetched_at.desc())
            .limit(CORPUS_SIZE)
            .all())
    df: Counter = Counter()
    for title, description in rows:
        df.update(set(tokens(f"{title} {description}")))
    with _lock:
        _cache["df"] = (dict(df), len(rows), time.monotonic())
    return dict(df), len(rows)


def _vector(words: list[str], df: dict, n_docs: int) -> dict:
    counts = Counter(words)
    return {w: (1 + math.log(c)) * math.log((n_docs + 1) / (df.get(w, 0) + 1))
            for w, c in counts.items()}


def cosine(a: dict, b: dict) -> float:
    if not a or not b:
        return 0.0
    small, large = (a, b) if len(a) < len(b) else (b, a)
    dot = sum(v * large.get(k, 0.0) for k, v in small.items())
    norm = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values()))
    return dot / norm if norm else 0.0


class Scorer:
    """The profile's vector, built once, against any number of postings."""

    def __init__(self, profile_data: dict, df: dict, n_docs: int):
        self.df, self.n_docs = df, n_docs
        self.profile = _vector(tokens(profile_text(profile_data)), self.df, self.n_docs)

    def score(self, job) -> int | None:
        """0–100: the cosine, stretched so a typical good fit reads in the middle."""
        if self.n_docs < 2 or not self.profile:
            return None  # no corpus to weigh words by, or nothing in the profile
        value = cosine(self.profile, _vector(tokens(job_text(job)), self.df, self.n_docs))
        # Cosines between a resume and a posting sit in roughly 0–0.4; the
        # square root spreads that range across the scale without reordering.
        return int(round(min(1.0, math.sqrt(value / 0.4)) * 100)) if value > 0 else 0


def scorer(db, profile_data: dict) -> Scorer:
    df, n = document_frequencies(db)
    return Scorer(profile_data, df, n)


def reset_cache() -> None:
    with _lock:
        _cache.clear()
