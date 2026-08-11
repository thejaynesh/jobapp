"""
What a posting says about who is allowed to hold the role.

Two findings come out of one pass, and they are deliberately not the same kind
of thing:

  restriction   — the posting states the role is closed to non-citizens. That is
                  unwinnable regardless of merit, so it filters the job.
  sponsorship   — the posting says something about visa sponsorship, either way.
                  That is *surfaced and never acted on*: the job keeps its place
                  in the list, keeps its score, and ranks as if the sentence
                  weren't there. It exists so the reader can weigh it.

Both quote the posting's own sentence back. Neither infers anything about the
candidate, and neither reaches an LLM — this is string matching over the
employer's stated terms, which is why it can be trusted to run on every job.

The hard part is not finding the phrases, it's *not* firing on the many places
they legitimately appear. Three guards do that work: EEO boilerplate is skipped
wholesale (it enumerates "citizenship status" precisely because it is not
allowed to discriminate on it), negated statements are skipped ("no clearance
required" is the opposite of a clearance requirement), and the bare-word
landmines — "secret", "EAR" — are only matched in the company of the words that
make them mean what we think they mean.
"""

import re
from dataclasses import dataclass

# Sentences carrying any of these are equal-opportunity boilerplate. They list
# citizenship and national origin *because* the employer is promising not to
# select on them, so matching a restriction here gets the meaning exactly
# backwards.
_EEO_MARKERS = (
    "equal opportunity", "equal employment", "regardless of", "without regard",
    "discriminat", "protected veteran", "protected class", "affirmative action",
    "we celebrate", "diversity", "eeo", "e-verify",
)

# Phrases that flip a requirement into its absence: "no clearance required",
# "clearance is not necessary". Checked within the matched sentence only.
_NEGATION_RE = re.compile(
    r"\b(?:not\s+(?:required|require|needed|necessary|mandatory)"
    r"|no\s+(?:security\s+)?clearance"
    r"|without\s+(?:a\s+)?clearance"
    r"|is\s?n[o']t\s+required"
    r"|do(?:es)?\s?n[o']t\s+(?:need|require)"
    r"|nice\s+to\s+have"
    r"|preferred\s+but\s+not)\b",
    re.I,
)

# --- Tier 1: blocking. The posting says citizens only. ----------------------
#
# Case-sensitive patterns are listed separately: "ITAR" and "EAR" as acronyms
# are meaningful, but matched case-insensitively "ear" hits every "year",
# "search" and "clear" in the description.

_RESTRICTION_PATTERNS = [
    (re.compile(r"must be a[n]? (?:u\.?s\.?|united states) citizen", re.I),
     "US citizenship required"),
    (re.compile(r"(?:u\.?s\.?|united states) citizenship (?:is )?(?:required|mandatory)", re.I),
     "US citizenship required"),
    (re.compile(r"(?:u\.?s\.?|united states) citizens only", re.I),
     "US citizens only"),
    (re.compile(r"(?:restricted|limited|open only) to (?:u\.?s\.?|united states) citizens", re.I),
     "US citizens only"),
    (re.compile(r"must (?:be|hold|possess|have) .{0,40}?(?:security )?clearance", re.I),
     "Security clearance required"),
    (re.compile(r"(?:active|current|existing|valid)\s+(?:\w+\s+){0,3}?clearance", re.I),
     "Active security clearance required"),
    (re.compile(r"security clearance (?:is )?(?:required|mandatory)", re.I),
     "Security clearance required"),
    (re.compile(r"(?:top[\s-]secret|ts/sci)\b", re.I),
     "Security clearance required"),
    (re.compile(r"\bsecret\s+clearance\b", re.I),
     "Security clearance required"),
    (re.compile(r"\bu\.?s\.?\s+person(?:s)?\b", re.I),
     "ITAR / US Person requirement"),
    (re.compile(r"export[\s-]control(?:led|s)?\b", re.I),
     "Export-control restriction"),
]

_RESTRICTION_PATTERNS_CASED = [
    (re.compile(r"\bITAR\b"), "ITAR / US Person requirement"),
    (re.compile(r"\bEAR\b"), "Export-control restriction"),
]

# --- Tier 2: advisory. The posting says something about sponsorship. --------

_SPONSORSHIP_TRIGGER = re.compile(r"sponsor(?:s|ed|ing|ship)?\b", re.I)

# A sponsorship sentence is negative if it is negated, positive otherwise.
# Reading the negation rather than enumerating every phrasing is what lets
# "we are unable to offer sponsorship at this time" and "must not require
# sponsorship now or in the future" both land in the right bucket.
#
# Word boundaries are load-bearing throughout: as bare substrings, "no" matches
# "now", and "except" matches "exceptional", which turns an offer of sponsorship
# into a refusal of it.
_SPONSORSHIP_NEGATION_RE = re.compile(
    r"\b(?:not|no|cannot|can\s?n[o']t|wo\s?n[o']t|will\s+not|unable|unwilling"
    r"|without|ineligible|neither|nor|excluding|regret|unfortunately"
    r"|do(?:es)?\s?n[o']t)\b",
    re.I,
)

_SPONSORSHIP_POSITIVE_RE = re.compile(
    r"\b(?:available|offers?|offering|provides?|provided|providing"
    r"|supports?|willing|open\s+to|eligible\s+for|considers?|happy\s+to)\b",
    re.I,
)

MAX_QUOTE_CHARS = 300


@dataclass(frozen=True)
class EligibilityScan:
    """What the posting stated, if anything."""

    # Tier 1 — blocks the job. `label` is the short reason, `quote` the sentence.
    restriction_label: str | None = None
    restriction_quote: str | None = None

    # Tier 2 — displayed only. Never a filter, score or ranking input.
    sponsorship_note: str | None = None
    sponsorship_direction: str | None = None  # "negative" | "positive"

    @property
    def blocked(self) -> bool:
        return self.restriction_label is not None


# "U.S." ends in a period that is not the end of a sentence. Splitting there
# tears "must be a U.S. citizen" into two fragments, neither of which matches
# anything — which is the difference between catching a restriction and missing
# it. The dots are hidden during the split and restored afterwards.
_ABBREVIATIONS = re.compile(
    r"\b(?:U\.S\.A|U\.S|U\.K|E\.U|D\.C|e\.g|i\.e|etc|vs|approx|Inc|Ltd|Corp"
    r"|Co|Dr|Mr|Mrs|Ms|Jr|Sr|Ph\.D|B\.S|M\.S)\.",
    re.I,
)
_DOT_SENTINEL = "\x00"

# Leading list markers are part of the layout, not the sentence.
_LIST_MARKER = re.compile(r"^[\s•·▪●○*\-–—]+")

# A line that opens with a marker is a new list item, never a continuation.
_LIST_ITEM = re.compile(r"^\s*[•·▪●○*\-–—]\s")

# Hard-wrapped prose only counts as wrapped if the previous line was long enough
# to have been wrapped. Without that test a short heading glues itself onto the
# sentence beneath it, and the quote opens with the job title.
_WRAP_MIN_LINE = 50


def _unwrap(text: str) -> str:
    """
    Rejoin lines that a hard wrap split mid-sentence.

    Job descriptions arrive wrapped at 70-odd columns, so one sentence routinely
    spans several lines. Newlines still have to break bullet lists apart, but
    breaking on every one of them truncates the quote mid-clause — "must be
    authorized to work without sponsorship" loses the "now or in the future"
    that gives it its point.
    """
    lines: list[str] = []
    for raw in text.split("\n"):
        stripped = raw.strip()
        continues = (
            lines
            and stripped
            and len(lines[-1]) >= _WRAP_MIN_LINE
            and not re.search(r"[.!?:;]$", lines[-1])
            and not _LIST_ITEM.match(raw)
        )
        if continues:
            lines[-1] = f"{lines[-1]} {stripped}"
        else:
            lines.append(stripped)
    return "\n".join(lines)


def _sentences(text: str) -> list[str]:
    """
    Split a job description into quotable fragments.

    Bullet lists frequently carry the restriction and frequently have no
    terminating punctuation, so newlines split as well as sentence enders.
    Semicolons deliberately do *not* split: they join clauses that share a
    subject, and separating them strands the second half from the context that
    explains it — an EEO sentence reads as a restriction once cut in half.
    """
    if not text:
        return []
    protected = _ABBREVIATIONS.sub(
        lambda m: m.group(0).replace(".", _DOT_SENTINEL), _unwrap(text)
    )
    parts = re.split(r"(?<=[.!?])\s+|\n+", protected)
    out = []
    for part in parts:
        part = _LIST_MARKER.sub("", part.replace(_DOT_SENTINEL, "."))
        part = " ".join(part.split())
        if part:
            out.append(part)
    return out


def _is_boilerplate(sentence: str) -> bool:
    low = sentence.lower()
    return any(marker in low for marker in _EEO_MARKERS)


def _is_negated(sentence: str) -> bool:
    return bool(_NEGATION_RE.search(sentence))


def _quote(sentence: str) -> str:
    sentence = " ".join(sentence.split())
    if len(sentence) <= MAX_QUOTE_CHARS:
        return sentence
    return sentence[: MAX_QUOTE_CHARS - 1].rstrip() + "…"


def _find_restriction(sentences: list[str]) -> tuple[str | None, str | None]:
    for sentence in sentences:
        if _is_boilerplate(sentence) or _is_negated(sentence):
            continue
        for pattern, label in _RESTRICTION_PATTERNS:
            if pattern.search(sentence):
                return label, _quote(sentence)
        for pattern, label in _RESTRICTION_PATTERNS_CASED:
            if pattern.search(sentence):
                return label, _quote(sentence)
    return None, None


def _classify_sponsorship(sentence: str) -> str:
    if _SPONSORSHIP_NEGATION_RE.search(sentence):
        return "negative"
    if _SPONSORSHIP_POSITIVE_RE.search(sentence):
        return "positive"
    # A bare mention with neither negation nor an offer ("sponsorship policy
    # varies by role") is more likely to be a caveat than an offer, and the
    # cautious reading is the one worth showing.
    return "negative"


def _find_sponsorship(sentences: list[str]) -> tuple[str | None, str | None]:
    """
    The sponsorship statement, preferring a negative one when both appear.

    Postings sometimes carry both ("sponsorship available for some roles; this
    one is not eligible"). The constraint is the part worth surfacing.
    """
    positive: tuple[str, str] | None = None
    for sentence in sentences:
        if _is_boilerplate(sentence) or not _SPONSORSHIP_TRIGGER.search(sentence):
            continue
        direction = _classify_sponsorship(sentence)
        if direction == "negative":
            return _quote(sentence), "negative"
        if positive is None:
            positive = (_quote(sentence), direction)
    return positive if positive else (None, None)


def scan(description: str | None) -> EligibilityScan:
    """Read a job description for stated eligibility terms."""
    sentences = _sentences(description or "")
    if not sentences:
        return EligibilityScan()

    label, quote = _find_restriction(sentences)
    note, direction = _find_sponsorship(sentences)
    return EligibilityScan(
        restriction_label=label,
        restriction_quote=quote,
        sponsorship_note=note,
        sponsorship_direction=direction,
    )
