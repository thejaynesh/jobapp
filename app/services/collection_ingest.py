"""One idempotent store path for normal fetches and durable batch recovery."""
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError

from app.models.job import Job, JobStatus
from app.services import posting_identity
from app.services.deduplication import (
    compute_dedupe_hash, enrich_from, find_existing_job, merge_description,
    note_addresses, note_source, was_archived,
)
from app.services.descriptions import clean
from app.services.listing_observations import observe, parse_time


def store(db, data: dict, *, max_age_days=0, now=None, known=None) -> tuple[str, Job | None]:
    """Store one exact posting under a savepoint; concurrent winners are merged."""
    now = now or datetime.now(timezone.utc)
    source, url = data.get("source") or "", (data.get("url") or "").strip()
    if not source or not url or not (data.get("title") or "").strip():
        raise ValueError("A posting requires a source, title and URL")
    company, title, location = data.get("company") or "", data["title"], data.get("location") or ""
    posted = parse_time(data.get("posted_at"))
    if posted and max_age_days and (now - posted).total_seconds() > float(max_age_days) * 86400:
        return "stale", None
    source_id = data.get("source_job_id")
    if source_id is not None:
        source_id = str(source_id)
    normalized = {**data, "source_job_id": source_id, "posted_at": posted,
                  "description": clean(data.get("description") or "")}
    fingerprint = compute_dedupe_hash(company, title, location, url)
    key = posting_identity.job_key(source, url, source_id, data.get("apply_url"), data.get("ats_slug"))
    for attempt in range(2):
        try:
            with db.begin_nested():
                # Consistent key serializes same-posting arrivals even where
                # a legacy row has no unique identity_key yet.
                from sqlalchemy import text
                db.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                           {"key": int(key[:16], 16) - 2**63})
                if known is not None and attempt == 0:
                    existing_id = known.existing_id(source, url, source_id, fingerprint, data.get("apply_url"))
                    job = db.get(Job, existing_id) if existing_id is not None else None
                    if job is not None and posting_identity.conflicts(job, source, url, source_id, data.get("apply_url")):
                        job = find_existing_job(db, source, url, source_id, fingerprint, data.get("apply_url"))
                else:
                    job = find_existing_job(db, source, url, source_id, fingerprint, data.get("apply_url"))
                if job is None and (known is None or attempt):
                    job = db.query(Job).filter(Job.identity_key == key).first()
                if job is not None:
                    improved = observe(db, job, normalized, now)
                    if not normalized.get("_stale_revision"):
                        improved.extend(enrich_from(job, normalized))
                    elif normalized.get("_stale_observation_only"):
                        improved.extend(enrich_from(job, normalized, missing_only=True))
                    note_source(job, source)
                    if job.source == source and source_id and url in (job.source_urls or []):
                        job.source_job_id = source_id
                    if note_addresses(job, url, data.get("apply_url")):
                        improved.append("source_urls")
                    if not normalized.get("_stale_revision") and source not in posting_identity.AUTHORITATIVE_SOURCES and merge_description(job, normalized["description"]):
                        improved.append("description")
                    from app.services.job_fetcher import _note_board
                    if not normalized.get("_stale_revision"):
                        _note_board(job, normalized)
                    if job.identity_key is None:
                        job.identity_key = key
                    db.flush()
                    if known is not None:
                        known.remember_sighting(job, normalized)
                    return ("merged" if improved else "skipped"), job
                archived = (known.archived(source, url, source_id, fingerprint, data.get("apply_url"))
                            if known is not None and attempt == 0 else
                            was_archived(db, source, url, source_id, fingerprint, data.get("apply_url")))
                if archived:
                    return "skipped", None
                from app.services.job_fetcher import _adapter_details, _board_key
                job = Job(source=source, source_job_id=source_id,
                    source_urls=posting_identity.urls(url, data.get("apply_url")), seen_by=[source],
                    title=title, company=company, location=location,
                    is_remote=bool(data.get("is_remote")), url=url, apply_url=data.get("apply_url"),
                    description=normalized["description"] or None,
                    experience_level=data.get("experience_level"), status=JobStatus.new,
                    fetched_at=now, last_seen_at=now, posted_at=posted,
                    dedupe_hash=fingerprint, identity_key=key, board=_board_key(normalized),
                    **_adapter_details(data))
                db.add(job)
                db.flush()
                observe(db, job, normalized, now)
                enrich_from(job, normalized)
                db.flush()
                if known is not None:
                    known.remember_sighting(job, normalized)
                return "inserted", job
        except IntegrityError:
            if attempt:
                raise
            # The savepoint rollback restores the transaction; READ COMMITTED
            # sees the concurrent winner on the second exact-identity lookup.
    raise RuntimeError("unreachable")
