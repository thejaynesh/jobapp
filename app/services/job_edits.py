"""
Fields a person set by hand, and the promise that nothing overwrites them.

Every other writer to `jobs` is automatic — a source adapter, a cross-post
merge, the browser harvest, an enrichment pass — and all of them decide whether
to keep what they found by comparing lengths or checking for null. Between two
machines that is the right rule: the longer description is almost always the
fuller one, and a null column is a column nobody has filled.

Against a person it is the wrong rule entirely. The case this exists for is the
user opening the posting, seeing the real description, and pasting it in. That
text is *correct*, and it routinely loses on length to the boilerplate-padded
listing page enrichment scrapes on its next pass. Losing it silently is worse
than never having offered the edit.

So the rule here is simple and absolute: **a field named in `job.manual_fields`
is not written by anything automatic again.** Not when the new value is longer,
not when the old one is null, not ever — until the user releases it.

Three things this deliberately does not do
------------------------------------------
*It does not recompute `dedupe_hash`.* That hash is an identity fingerprint
built from what the source said, and it is how a cross-post arriving tomorrow
recognises this row. Rewriting it from corrected values would either collide
with a genuinely different job or orphan this one from its own duplicates, and
a tidier hash is not worth either.

*It does not re-score on its own beyond what enrichment already would.* An
edited description follows exactly the rule a grown one follows, so a job
filtered for `no_description` goes back in the queue and a job the user filtered
by hand stays filtered. Anything outside that is what the Re-match button is
for — an explicit click, on a job the user is already looking at.

*It does not clean text it did not have to.* `descriptions.clean` exists to
turn source soup into prose, and part of its job is returning "" for a
challenge page. Applied to a paste it would silently blank a real description
that happens to quote the words "access denied", so it runs only on input that
actually looks like markup, and never gets to empty a non-empty edit.
"""

import logging
import re
from datetime import datetime, timezone

from app.models.job import Job, JobStatus

logger = logging.getLogger(__name__)

# Enough new text to count as a different description — the same threshold
# enrichment uses, so "the docs were written against a thinner posting" means
# one thing across the app rather than two.
MEANINGFUL_CHANGE_CHARS = 200

_MAX_DESCRIPTION_CHARS = 200_000
_MAX_SHORT_CHARS = 500

# Markup in the paste. Only then is `clean` worth running: everything else is
# prose the user typed, and prose should arrive the way it was written.
_LOOKS_LIKE_MARKUP = re.compile(r"</?(p|div|br|li|ul|ol|span|h[1-6]|strong|em)\b", re.I)


class EditError(ValueError):
    """A value the user typed that cannot be stored. The message is for them."""


def _text(raw, limit: int = _MAX_SHORT_CHARS) -> str:
    return str(raw or "").strip()[:limit]


def _description(raw) -> str:
    """
    A pasted description, kept as close to what was pasted as possible.

    Cleaned only when it carries markup — someone copying out of a page's HTML
    source rather than off the rendered page. And never emptied: `clean`
    returns "" for anything it reads as a challenge page, which is correct for
    a scrape and wrong for a paste that quotes one.
    """
    text = str(raw or "").strip()[:_MAX_DESCRIPTION_CHARS]
    if not text or not _LOOKS_LIKE_MARKUP.search(text):
        return text

    from app.services.descriptions import clean

    try:
        cleaned = clean(text)
    except Exception as exc:  # pragma: no cover - clean() is defensive already
        logger.warning("job_edits: could not clean a pasted description: %s", exc)
        return text
    return cleaned or text


def _url(raw) -> str | None:
    # None rather than "" when cleared: every reader treats this column as
    # "set or not", and an empty string is neither.
    url = _text(raw)
    if not url:
        return None
    if not url.lower().startswith(("http://", "https://")):
        raise EditError("A link has to start with http:// or https://")
    return url


def _money(raw) -> float | None:
    text = str(raw or "").strip().replace(",", "").replace("$", "")
    if not text:
        return None
    try:
        amount = float(text)
    except ValueError as exc:
        raise EditError(f"{raw!r} is not a number") from exc
    if amount < 0:
        raise EditError("Pay cannot be negative")
    return amount or None


def _flag(raw) -> bool:
    return str(raw or "").strip().lower() in ("1", "true", "on", "yes")


def _choice(options: tuple, allow_blank: bool = True):
    def parse(raw):
        value = _text(raw).lower()
        if not value:
            if allow_blank:
                return None
            raise EditError("Pick one of the options")
        if value not in options:
            raise EditError(f"{raw!r} is not one of: {', '.join(options)}")
        return value

    return parse


def _required(raw):
    value = _text(raw, 300)
    if not value:
        raise EditError("This cannot be left empty")
    return value


EMPLOYMENT_TYPES = ("full_time", "part_time", "contract", "internship")
EXPERIENCE_LEVELS = ("entry", "mid", "senior")

# What the form offers, in the order it offers it. `parse` turns whatever the
# form posted into what the column holds, and raises `EditError` with a
# sentence the user can act on when it cannot.
#
# `url` is absent on purpose: it is the key three quarters of deduplication is
# built on, and correcting a typo in it would detach the job from its own
# cross-posts. `apply_url` is the one worth fixing by hand anyway — it is what
# the Apply link uses and what enrichment fetches.
EDITABLE: dict[str, dict] = {
    "description": {
        "label": "Job description",
        "kind": "textarea",
        "parse": _description,
        "help": "Paste the real posting here. Nothing automatic will overwrite it.",
    },
    "title": {"label": "Title", "kind": "text", "parse": _required},
    "company": {"label": "Company", "kind": "text", "parse": _required},
    "location": {"label": "Location", "kind": "text", "parse": _text},
    "apply_url": {
        "label": "Apply link",
        "kind": "url",
        "parse": _url,
        "help": "The employer's own application page, when the job link lands on a board.",
    },
    "is_remote": {"label": "Remote", "kind": "checkbox", "parse": _flag},
    "employment_type": {
        "label": "Employment type",
        "kind": "choice",
        "options": EMPLOYMENT_TYPES,
        "parse": _choice(EMPLOYMENT_TYPES),
    },
    "experience_level": {
        "label": "Experience level",
        "kind": "choice",
        "options": EXPERIENCE_LEVELS,
        "parse": _choice(EXPERIENCE_LEVELS),
    },
    "salary_min": {"label": "Pay from", "kind": "number", "parse": _money},
    "salary_max": {"label": "Pay to", "kind": "number", "parse": _money},
    "salary_currency": {
        "label": "Currency",
        "kind": "text",
        "parse": lambda raw: (_text(raw, 8).upper() or None),
    },
}

EDITABLE_FIELDS = tuple(EDITABLE)


# ---------------------------------------------------------------------------
# What automation is allowed to touch
# ---------------------------------------------------------------------------

def is_manual(job: Job, field: str) -> bool:
    """
    Whether the user set this field by hand.

    The one function every automatic writer calls before overwriting. Tolerant
    of a job object that predates the column (a hand-built test double, a row
    loaded before a migration) because refusing to answer would fail the write
    it was asked to guard.
    """
    return field in (getattr(job, "manual_fields", None) or [])


def locked(job: Job) -> list[str]:
    """Every field on this job that automation must leave alone."""
    return list(getattr(job, "manual_fields", None) or [])


def release(job: Job, fields) -> list[str]:
    """
    Hand fields back to automation. Returns what was actually released.

    The counterpart to editing, and the reason the lock can be absolute: a
    permanent decision the user cannot undo would make people avoid the edit
    button entirely.
    """
    wanted = {f for f in fields if f in EDITABLE}
    current = locked(job)
    remaining = [f for f in current if f not in wanted]
    if len(remaining) != len(current):
        job.manual_fields = remaining
    return [f for f in current if f in wanted]


# ---------------------------------------------------------------------------
# Making an edit
# ---------------------------------------------------------------------------

def parse(values: dict) -> tuple[dict, dict]:
    """
    Turn a posted form into column values. Returns `(parsed, errors)`.

    Every field is validated before any is applied, so a bad salary does not
    leave a half-saved description behind it.
    """
    parsed: dict = {}
    errors: dict = {}
    for field, spec in EDITABLE.items():
        if field not in values:
            continue
        try:
            parsed[field] = spec["parse"](values[field])
        except EditError as exc:
            errors[field] = str(exc)
    return parsed, errors


def apply(db, job: Job, values: dict, release_fields=()) -> dict:
    """
    Store hand-edited values on `job` and lock what changed.

    Only fields whose value actually differs are locked. Opening the form,
    changing the description and pressing save should not freeze the title
    against every future correction as a side effect — the user did not edit
    it, they just did not delete it.

    Does not commit: the caller owns the transaction, so a failure anywhere in
    the request leaves the job exactly as it was.
    """
    parsed, errors = parse(values)
    if errors:
        raise EditError("; ".join(f"{EDITABLE[f]['label']}: {m}" for f, m in errors.items()))

    released = release(job, release_fields)

    old_description = job.description or ""
    changed: list[str] = []
    for field, value in parsed.items():
        if getattr(job, field) == value:
            continue
        setattr(job, field, value)
        changed.append(field)

    if not changed and not released:
        return {"changed": [], "released": [], "requeued": False,
                "description_changed": False, "chars_gained": 0}

    now = datetime.now(timezone.utc)
    job.edited_at = now
    # Lock what changed, keep what was already locked, drop what was released
    # in this same submission.
    job.manual_fields = sorted(set(locked(job)) | set(changed))

    outcome = {
        "changed": changed, "released": released, "requeued": False,
        "description_changed": "description" in changed,
        "chars_gained": len(job.description or "") - len(old_description),
    }

    if outcome["description_changed"]:
        # The same stamp a grown description gets. Documents written against
        # the old text are stale whether a scraper or a person replaced it.
        if abs(outcome["chars_gained"]) >= MEANINGFUL_CHANGE_CHARS:
            job.description_updated_at = now
        if _worth_rescoring(job):
            job.status = JobStatus.new
            job.filter_reason = None
            job.filter_detail = None
            outcome["requeued"] = True

    logger.info(
        "job_edits: %s edited by hand (%s)%s",
        job.id, ", ".join(changed) or "nothing changed",
        " — requeued for matching" if outcome["requeued"] else "",
    )
    return outcome


def _worth_rescoring(job: Job) -> bool:
    """
    Whether an edited description should send this job back to be scored.

    Deliberately the same rule enrichment applies to a grown one, imported
    rather than restated so the two cannot drift: only verdicts that were
    reached by reading a description, and never a job that already carries an
    application.

    A verdict the user made by hand is left alone here too. Editing the
    description of a job you filtered yourself is not the same as changing your
    mind about it, and Re-match is one click away for when it is.
    """
    from app.services.enrichment import _worth_rescoring as enrichment_rule

    return enrichment_rule(job)
