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
    # The four below name a thing rather than state a rule. They stay in the
    # blocking tier — "this position involves export-controlled technical data"
    # is a real constraint — but `_ABOUT_THE_EMPLOYER_RE` now keeps them from
    # firing on the paragraph describing what the company sells.
    (re.compile(r"(?:top[\s-]secret|ts/sci)\b", re.I),
     "Security clearance required"),
    (re.compile(r"\bsecret\s+clearance\b", re.I),
     "Security clearance required"),
    (re.compile(r"\bu\.?s\.?\s+person(?:s)?\b", re.I),
     "ITAR / US Person requirement"),
    (re.compile(r"export[\s-]control(?:led|s)?\b", re.I),
     "Export-control restriction"),
]

# Sentences that are about the employer rather than about the reader.
#
# The last four patterns above name a thing rather than state a rule, and the
# thing turns up constantly in the paragraph describing what the company
# sells:
#
#   "Acme builds software that helps manufacturers manage export control."
#   "Our TS/SCI-cleared customers rely on us."
#   "Acme collects personal data about U.S. persons under CCPA."
#
# Every one of those was filtered as "Restricted to US citizens" — the
# blocking tier, the one with the irreversible consequence. So this did not
# lose jobs, it lost employers: every posting at a trade-compliance vendor, and
# every posting at a security company whose customers hold clearances.
#
# The discriminator is the subject of the sentence, not the presence of
# requirement language. "This position involves export-controlled technical
# data" is about the role and still blocks, which is the behaviour this module
# has always had and `TestCitizenshipRestriction` asserts. What changes is that
# a sentence visibly about the company, its product or its customers no longer
# counts as a statement about who may hold the job.
#
# An exclusion list, deliberately, for the same reason as
# `_NOT_IMMIGRATION_RE` below: it narrows only the demonstrated false
# positives and leaves every other reading as conservative as it was.
_ABOUT_THE_EMPLOYER_RE = re.compile(
    r"\b(?:about\s+us"
    r"|our\s+(?:product|products|platform|software|tool|tools|customers|"
    r"clients|mission|company|technology\s+helps|team\s+supports)"
    r"|we\s+(?:build|built|help|helps|provide|serve|offer|make|sell)"
    r"|helps?\s+(?:companies|manufacturers|customers|clients|teams|"
    r"organi[sz]ations|enterprises|businesses)"
    r"|collects?\s+personal\s+data"
    r"|under\s+(?:ccpa|gdpr|hipaa)"
    r"|[\w-]+-cleared\s+(?:customers|clients|users))\b",
    re.I,
)

_RESTRICTION_PATTERNS_CASED = [
    (re.compile(r"\bITAR\b"), "ITAR / US Person requirement"),
    (re.compile(r"\bEAR\b"), "Export-control restriction"),
]

# --- Tier 2: advisory. The posting says something about sponsorship. --------

_SPONSORSHIP_TRIGGER = re.compile(r"sponsor(?:s|ed|ing|ship)?\b", re.I)

# "Sponsor" is not an immigration word on its own, and the two places it turns
# up in a job posting mean opposite things:
#
#   "We sponsor attendance at PyCon and regional tech conferences."
#   "The executive sponsor will oversee delivery."
#
# The first matched the positive pattern on "supports" and was badged
# *sponsorship available*; the second matched nothing and was badged *will not
# sponsor*. Both are wrong about something the posting never discussed, and the
# badge is shown in four places, so it asserted it four times in the
# employer's name.
#
# So a sentence has to plausibly be about immigration before its direction is
# read. An *exclusion* list rather than a required inclusion list, and that
# choice is the whole design.
#
# In a job posting, "sponsorship" unqualified is immigration — real statements
# say "Candidates must not require sponsorship" and "Sponsorship provided for
# the right candidate" with no other immigration word anywhere in the sentence.
# Requiring one of those words would trade these false positives for false
# negatives on the actual statements, which is the worse trade: a missing badge
# is a job the user has to read the posting for, and a wrong badge is the
# product asserting something the employer did not say.
#
# What the false positives have in common instead is a visible non-immigration
# object: a conference, a project, a person holding a role. Naming those is
# narrow, checkable, and fails open the right way.
_NOT_IMMIGRATION_RE = re.compile(
    r"\b(?:conference|conferences|event|events|meetup|meetups|summit|talk|talks"
    r"|booth|open[\s-]?source|project|projects|charity|charities|nonprofit"
    r"|community|bootcamp|hackathon|gym|tuition|course|courses|membership"
    r"|executive|engineering|product|team|squad|program|programme|initiative"
    r"|workstream|stakeholder)\s+sponsor"
    # Up to three intervening words, so "sponsor two open-source projects"
    # reads the same as "sponsor projects".
    r"|sponsor(?:s|ed|ing|ship)?\s+(?:\w+[\s-]+){0,3}?"
    r"(?:attendance|travel|tickets?|conferences?|learning|education"
    r"|source|projects?|events?|meetups?|charities|hackathons?|booths?)\b",
    re.I,
)

# Where a clause ends, for reading direction. A negation belonging to one
# clause says nothing about the next: "Although we cannot offer relocation
# assistance, visa sponsorship is available" was read as a refusal because
# "cannot" appeared somewhere in the sentence.
_CLAUSE_SPLIT_RE = re.compile(
    r"(?:[,;:]|\b(?:but|although|though|however|whereas|while)\b)", re.I
)

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
        # A sentence about the employer's product or customers is not a
        # statement about who may hold the job. See `_ABOUT_THE_EMPLOYER_RE`.
        if _ABOUT_THE_EMPLOYER_RE.search(sentence):
            continue
        for pattern, label in _RESTRICTION_PATTERNS:
            if pattern.search(sentence):
                return label, _quote(sentence)
        for pattern, label in _RESTRICTION_PATTERNS_CASED:
            if pattern.search(sentence):
                return label, _quote(sentence)
    return None, None


def _sponsorship_clause(sentence: str) -> str:
    """
    The part of the sentence that is actually about sponsorship.

    Direction used to be read from the whole sentence, so a negation belonging
    to a different clause flipped the answer — and "provided at no cost to the
    candidate" read as a refusal on the strength of the words "no cost".
    """
    clauses = [c for c in _CLAUSE_SPLIT_RE.split(sentence) if c and c.strip()]
    owning = [c for c in clauses if _SPONSORSHIP_TRIGGER.search(c)]
    return " ".join(owning) if owning else sentence


def _classify_sponsorship(sentence: str) -> str:
    """
    Which way the sponsorship clause points.

    Both patterns routinely match the same clause, so the question is which one
    *governs* — and word order answers it. A negation before the offer verb
    negates it ("we are **unable** to *provide* sponsorship"); a negation after
    it belongs to something else ("sponsorship is *provided* at **no** cost").
    Testing either one first in isolation gets one of those two backwards.
    """
    clause = _sponsorship_clause(sentence)
    offer = _SPONSORSHIP_POSITIVE_RE.search(clause)
    denial = _SPONSORSHIP_NEGATION_RE.search(clause)

    if offer and denial:
        return "negative" if denial.start() < offer.start() else "positive"
    if offer:
        return "positive"
    # A bare mention with neither negation nor an offer ("sponsorship policy
    # varies by role") is more likely to be a caveat than an offer, and the
    # cautious reading is the one worth showing. Same default for an explicit
    # denial.
    return "negative"


def _find_sponsorship(sentences: list[str]) -> tuple[str | None, str | None]:
    """
    The sponsorship statement, preferring a negative one when both appear.

    Postings sometimes carry both ("sponsorship available for some roles; this
    one is not eligible"). The constraint is the part worth surfacing.

    A sentence that is visibly about something other than immigration does not
    count at all — see `_NOT_IMMIGRATION_RE`. Without that test, conference
    sponsorship and project sponsors were being reported as visa policy.
    """
    positive: tuple[str, str] | None = None
    for sentence in sentences:
        if _is_boilerplate(sentence) or not _SPONSORSHIP_TRIGGER.search(sentence):
            continue
        if _NOT_IMMIGRATION_RE.search(sentence):
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
