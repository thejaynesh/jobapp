import hashlib
import re
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
    text = (location or "").strip()
    if not text:
        return ""
    if _REMOTE_LOCATION_RE.search(text):
        return "remote"
    # "San Francisco, CA, United States" and "San Francisco, CA" → "san francisco"
    first_segment = re.split(r"[,;/|]", text)[0]
    return " ".join(_tokens(first_segment))


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

    rows = (
        db.query(Job)
        .join(Application, Application.job_id == Job.id)
        .filter(Job.id != job.id)
        .all()
    )
    for candidate in rows:
        if normalize_company(candidate.company or "") != norm_company:
            continue
        cand_title = normalize_title(candidate.title or "")
        if cand_title == norm_title or SequenceMatcher(
            None, norm_title, cand_title
        ).ratio() >= TITLE_SIMILARITY_THRESHOLD:
            return candidate
    return None


# A description this much longer than the stored one counts as "meaningfully
# fuller": documents generated from the old text are flagged as predating it.
MEANINGFUL_DESCRIPTION_GROWTH = 200


def note_description_growth(job: Job, old_length: int) -> None:
    """Stamp the job when its description just got meaningfully fuller."""
    from datetime import datetime, timezone

    if len(job.description or "") - old_length >= MEANINGFUL_DESCRIPTION_GROWTH:
        job.description_updated_at = datetime.now(timezone.utc)


def merge_or_skip(
    db: Session,
    existing: Job,
    new_url: str,
    new_description: str,
    layer: int,
) -> None:
    """Update an existing job when a cross-post is found (layer=3)."""
    if new_url not in existing.source_urls:
        existing.source_urls = existing.source_urls + [new_url]

    old_length = len(existing.description or "")
    if len(new_description) > old_length:
        existing.description = new_description
        note_description_growth(existing, old_length)
