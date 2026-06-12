import hashlib
import re

from sqlalchemy.orm import Session

from app.models.job import Job


def _normalize(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def compute_dedupe_hash(company: str, title: str, location: str) -> str:
    payload = f"{_normalize(company)}|{_normalize(title)}|{_normalize(location)}"
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
