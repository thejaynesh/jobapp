import hashlib
import re
from datetime import datetime
from difflib import SequenceMatcher

from sqlalchemy.orm import Session

from app.models.job import Job

# How alike two normalized titles must be to count as the same posting when
# the dedupe hash missed (cross-posts routinely add "- Remote", reorder words,
# or expand an abbreviation, which all change the hash).
TITLE_SIMILARITY_THRESHOLD = 0.85

# Aggregators cross-post the same job with cosmetic differences; normalize the
# three hash inputs hard so "Stripe, Inc." / "Sr. Software Engineer" /
# "San Francisco, CA, United States" collide with their variants.

_COMPANY_SUFFIXES = frozenset({
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation",
    "co", "company", "gmbh", "bv", "sa", "plc", "pvt", "pte", "holdings",
})

_TITLE_TOKEN_MAP = {
    "sr": "senior",
    "jr": "junior",
    "engr": "engineer",
    "dev": "developer",
}

# Tokens that vary between postings of the same job (work mode / urgency tags).
_TITLE_DROP_TOKENS = frozenset({"remote", "hybrid", "onsite", "urgent", "fulltime"})

_REMOTE_LOCATION_RE = re.compile(r"remote|anywhere|worldwide|work from home|wfh", re.I)

# Tokens that say where a city is without saying which city it is: the country
# around it, the state around it, and the words a metro area wraps it in. See
# `normalize_location` for why dropping them by name is safe.
_LOCATION_NOISE = frozenset(
    {"usa", "us", "u", "s", "united", "states", "america"}
    | {"greater", "area", "metro", "metropolitan", "region", "county"}
    | {
        "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
        "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
        "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
        "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
        "wi", "wy", "dc",
    }
)


def _tokens(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (s or "").lower())


def normalize_company(company: str) -> str:
    tokens = _tokens(company)
    while len(tokens) > 1 and tokens[-1] in _COMPANY_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def normalize_title(title: str) -> str:
    tokens = [_TITLE_TOKEN_MAP.get(t, t) for t in _tokens(title)]
    tokens = [t for t in tokens if t not in _TITLE_DROP_TOKENS]
    return " ".join(tokens)


def normalize_location(location: str) -> str:
    """
    Reduce a posting's stated location to the city it names.

    A job's location is written differently by every board that lists it, and
    the differences are never a different job — but this is a third of the
    dedupe hash, so each unmatched formatting variant is one posting stored
    twice. Measured against real pairs, taking the first comma-segment alone
    missed three shapes:

        "US-MA-Boston"            vs "Boston, MA"     (country first, no comma)
        "United States - New York" vs "New York, NY"  (same, spelled out)
        "Greater Boston Area"      vs "Boston, MA"    (metro wrapper)

    So the country, the state and the metro-area filler are dropped by name.
    The country-first forms fall out of that for free: tokenising already
    splits on the dashes, and once "us" and "ma" are gone what is left of
    "US-MA-Boston" is the city.
    """
    text = (location or "").strip()
    if not text:
        return ""
    if _REMOTE_LOCATION_RE.search(text):
        return "remote"
    # "San Francisco, CA, United States" and "San Francisco, CA" → the city is
    # the first segment. In the country-first forms there is no comma at all,
    # and the dash split below is what separates them.
    first_segment = re.split(r"[,;/|]", text)[0]
    tokens = _tokens(first_segment)
    kept = [t for t in tokens if t not in _LOCATION_NOISE]
    # Falling back is what makes stripping by name safe. Several state
    # abbreviations are also cities — LA is Los Angeles as often as it is
    # Louisiana — and a location reduced to nothing would collide with every
    # other job at that company whose location also vanished, merging two real
    # postings into one and losing a job. Keeping the tokens is the worse
    # dedup and the safe failure.
    return " ".join(kept or tokens)


def compute_dedupe_hash(company: str, title: str, location: str) -> str:
    payload = (
        f"{normalize_company(company)}|{normalize_title(title)}|{normalize_location(location)}"
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def find_existing_job(
    db: Session,
    source: str,
    url: str,
    source_job_id: str | None,
    dedupe_hash: str,
) -> Job | None:
    # Layer 1: URL already in source_urls array
    job = db.query(Job).filter(Job.source_urls.any(url)).first()
    if job:
        return job

    # Layer 2: source + source_job_id match
    if source_job_id:
        job = (
            db.query(Job)
            .filter(Job.source == source, Job.source_job_id == source_job_id)
            .first()
        )
        if job:
            return job

    # Layer 3: content hash (cross-posted job)
    return db.query(Job).filter(Job.dedupe_hash == dedupe_hash).first()


def was_archived(
    db: Session,
    source: str,
    url: str,
    source_job_id: str | None,
    dedupe_hash: str,
) -> bool:
    """
    Whether this posting was already seen, judged, and retired.

    The same three layers as `find_existing_job`, against the tombstones left
    by `services.archive`. Without this check, archiving would be silently
    expensive rather than cheap: every archived posting still on its board gets
    re-inserted as brand new on the next fetch, costs a scoring call, reaches
    the same verdict it reached in June, and is archived again sixty days
    later. Forever.

    Returns a bool rather than a row on purpose. There is nothing to merge into
    — the description is exactly what archiving threw away — so the only honest
    answer to "have we seen this?" here is yes, and the caller skips it.
    """
    from app.models.archived_job import ArchivedJob

    query = db.query(ArchivedJob.id).filter(ArchivedJob.source_urls.any(url))
    if query.first():
        return True

    if source_job_id:
        query = db.query(ArchivedJob.id).filter(
            ArchivedJob.source == source,
            ArchivedJob.source_job_id == source_job_id,
        )
        if query.first():
            return True

    return db.query(ArchivedJob.id).filter(
        ArchivedJob.dedupe_hash == dedupe_hash
    ).first() is not None


def find_duplicate_application_job(db: Session, job) -> Job | None:
    """
    A different job, same employer, near-identical title, that already has an
    application.

    The dedupe hash catches exact-normalized matches at fetch time; what slips
    through is the cross-post with a cosmetic title difference ("Backend
    Engineer" vs "Backend Engineer - Remote"), and each one that slips through
    used to cost a full duplicate document generation. Compared in Python
    rather than SQL because normalization isn't expressible in a query, and
    the candidate set — jobs that actually have applications — is small.
    """
    norm_company = normalize_company(job.company or "")
    if not norm_company:
        return None
    norm_title = normalize_title(job.title or "")
    if not norm_title:
        return None

    from app.models.application import Application

    # Three columns, not whole rows. This runs once per matched job and the
    # candidate set is every job ever applied to, so hydrating full ORM objects
    # meant loading each one's description — and, through the selectin
    # relationship, its scores — to read a company and a title off it. That is
    # the pipeline's own backlog on the read side of a scoring pass, growing for
    # as long as the user keeps applying to things. The match is re-fetched
    # below, which is one row.
    rows = (
        db.query(Job.id, Job.company, Job.title)
        .join(Application, Application.job_id == Job.id)
        .filter(Job.id != job.id)
        .distinct()
        .all()
    )
    for candidate_id, company, title in rows:
        if normalize_company(company or "") != norm_company:
            continue
        cand_title = normalize_title(title or "")
        if cand_title == norm_title or SequenceMatcher(
            None, norm_title, cand_title
        ).ratio() >= TITLE_SIMILARITY_THRESHOLD:
            return db.get(Job, candidate_id)
    return None


# A description this much longer than the stored one counts as "meaningfully
# fuller": documents generated from the old text are flagged as predating it.
MEANINGFUL_DESCRIPTION_GROWTH = 200


def note_description_growth(job: Job, old_length: int) -> bool:
    """
    Stamp the job when its description just got meaningfully fuller, and let it
    be judged again. Returns whether it went back in the matching queue.

    The re-queue lives here rather than in each caller because this function is
    what "the description got fuller" means, and every path that fills one in
    should have the same consequence. Enrichment had it; a cross-post merge and
    the browser harvest did not — so a job filtered as `no_description` could
    have its description arrive from a harvest and sit there filtered out for
    exactly the thing that was no longer true. With twelve thousand LinkedIn
    jobs waiting on a harvest for their text, that gap was the difference
    between the feature working and the feature being decorative.

    The rule itself is enrichment's, imported rather than restated: only
    verdicts reached by reading a description, and never a job that already
    carries an application.
    """
    from datetime import datetime, timezone

    if len(job.description or "") - old_length < MEANINGFUL_DESCRIPTION_GROWTH:
        return False

    job.description_updated_at = datetime.now(timezone.utc)

    from app.models.job import JobStatus
    from app.services.enrichment import _worth_rescoring

    if not _worth_rescoring(job):
        return False
    job.status = JobStatus.new
    job.filter_reason = None
    job.filter_detail = None
    return True


# ---------------------------------------------------------------------------
# Taking the better half of two sightings
# ---------------------------------------------------------------------------
#
# The same posting reaches us from several places. LinkedIn's guest API knows
# the title and almost never the pay; the employer's own Greenhouse board knows
# the pay, the employment type and the day it went up; an aggregator card knows
# the direct apply link that LinkedIn buries behind a redirect. Each is missing
# something another one has, and which of them happened to be fetched first is
# an accident of scheduling.
#
# So a second sighting is not a duplicate to discard, it is the rest of the
# posting arriving late. What follows is the one place that decides what "more
# data" means, shared by every path that can see a job twice, because the rules
# only stay consistent if there is one copy of them.
#
# Two things are deliberately *not* merged:
#
# * `location`, because it is a third of the dedupe hash. A row whose stored
#   location no longer agrees with the hash computed from it is a row that
#   splits in two the next time the hashes are recomputed. This is the same
#   reason `url` is not user-editable.
# * `experience_level`, because both ingest paths default it to "mid" rather
#   than leaving it null. There is no absence to fill, only a guess to
#   overwrite with another guess.

# Fields where a stored null means "no source has told us yet", so the first
# source that does is strictly better than nothing.
_FILL_IF_NULL = (
    "employment_type",
    "posted_at",
    "required_years",
    "education_required",
    "benefits_note",
    "language",
)

# Same rule, for the list columns whose empty state is `[]` rather than null.
_FILL_IF_EMPTY = ("required_skills", "nice_to_have_skills")


def enrich_from(job: Job, data: dict) -> list[str]:
    """
    Take from a second sighting whatever the stored job is missing.

    Returns the names of the fields it filled, which is what lets a caller
    report "enriched" only when something actually improved. Never overwrites a
    value that is already there, and never touches a field the user has edited
    — `manual_fields` outranks every rule below.

    `data` uses the ingest dicts' own keys. `posted_at` must already be a
    datetime; a source's raw string is ignored rather than guessed at, because
    a mis-parsed date silently ages a job out of the pipeline.
    """
    from app.services.job_edits import is_manual

    filled: list[str] = []

    def take(field, value):
        if value is None or is_manual(job, field):
            return
        setattr(job, field, value)
        filled.append(field)

    # Pay moves as a unit. A minimum from one source and a maximum from another
    # is not a band anybody stated — it is two halves of two different bands,
    # and the salary filter would then drop jobs on a range nobody wrote down.
    incoming_min = data.get("salary_min")
    incoming_max = data.get("salary_max")
    if (incoming_min is not None or incoming_max is not None) \
            and job.salary_min is None and job.salary_max is None \
            and not is_manual(job, "salary_min") \
            and not is_manual(job, "salary_max"):
        job.salary_min = incoming_min
        job.salary_max = incoming_max
        job.salary_currency = data.get("salary_currency")
        filled.append("salary")

    # A resolved apply URL is the end of a redirect chain we followed once and
    # would rather not follow again, so this only ever fills a blank.
    apply_url = (data.get("apply_url") or "").strip()
    if apply_url and not job.apply_url and not is_manual(job, "apply_url"):
        job.apply_url = apply_url
        filled.append("apply_url")

    # A one-way ratchet, because the column cannot tell "not remote" from "the
    # source didn't say": it is a boolean defaulting to false. A source that
    # says remote is asserting something; a source that says nothing produces
    # exactly the same false. So true can be gained and never lost, which is
    # the only direction that cannot destroy information.
    if data.get("is_remote") and not job.is_remote and not is_manual(job, "is_remote"):
        job.is_remote = True
        filled.append("is_remote")

    for field in _FILL_IF_NULL:
        if getattr(job, field, None) is None:
            value = data.get(field)
            if field == "posted_at" and not isinstance(value, datetime):
                continue
            take(field, value)

    for field in _FILL_IF_EMPTY:
        if not getattr(job, field, None):
            value = data.get(field)
            if isinstance(value, (list, tuple)) and value:
                take(field, list(value))

    return filled


def merge_description(job: Job, new_description: str) -> bool:
    """
    Replace the stored description if this sighting carries a fuller one.

    Returns whether it did. The length comparison is the whole rule: between
    two machines, more text is more posting, and neither side has any way to
    judge quality. Against a person it is the wrong rule entirely, which is
    what the `manual_fields` check is for.
    """
    from app.services.job_edits import is_manual

    if is_manual(job, "description"):
        return False

    # Cleaned before the comparison, never after. Raw markup inflates a
    # description's length by roughly a third, so an HTML cross-post would beat
    # a longer plain-text description on the tape measure alone and replace
    # good text with worse.
    from app.services.descriptions import clean

    cleaned = clean(new_description)
    old_length = len(job.description or "")
    if len(cleaned) <= old_length:
        return False
    job.description = cleaned
    note_description_growth(job, old_length)
    return True


def merge_or_skip(
    db: Session,
    existing: Job,
    new_url: str,
    new_description: str,
    layer: int,
    data: dict | None = None,
) -> list[str]:
    """
    Fold a cross-post into the job it duplicates. Returns what it improved.

    `data` is the rest of the incoming sighting, and it is what makes this a
    merge rather than a URL append: without it the pay, the employment type and
    the posting date that this second source knew and the first did not were
    read, matched to an existing row, and dropped on the floor.
    """
    improved: list[str] = []

    # Recorded even when nothing else is: a second listing of the same job is a
    # real second listing, and this array is how the overlay finds the row from
    # whichever URL the user is looking at.
    if new_url not in existing.source_urls:
        existing.source_urls = existing.source_urls + [new_url]
        improved.append("source_urls")

    if data:
        improved.extend(enrich_from(existing, data))
    if merge_description(existing, new_description):
        improved.append("description")
    return improved
