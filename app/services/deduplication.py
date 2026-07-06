import hashlib
import re

from sqlalchemy.orm import Session

from app.models.job import Job

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

    if len(new_description) > len(existing.description or ""):
        existing.description = new_description
