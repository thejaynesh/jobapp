"""Source-specific sightings and corrections of the same exact posting."""
import hashlib
import json
from datetime import datetime, timezone

from app.config import live
from app.models.job import JobStatus
from app.models.source_listing import ListingRevision, SourceListing
from app.services import posting_identity
from app.services.descriptions import clean
from app.services.job_edits import is_manual


def parse_time(raw):
    if raw in (None, ""):
        return None
    try:
        if isinstance(raw, (int, float)):
            return datetime.fromtimestamp(raw, timezone.utc)
        value = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


_FIELDS = ("title", "company", "location", "is_remote", "salary_min", "salary_max",
           "salary_currency", "salary_period", "employment_type", "experience_level")


def observe(db, job, data: dict, now=None) -> list[str]:
    """Record a sighting; only an employer reader can correct stated fields."""
    now = now or datetime.now(timezone.utc)
    source, url = data.get("source") or job.source, data.get("url") or job.url
    key = posting_identity.listing_key(source, url, data.get("source_job_id"), data.get("ats_slug"))
    listing = db.get(SourceListing, key)
    if listing is None:
        listing = SourceListing(identity_key=key, job_id=job.id, source=source,
            board=data.get("ats_slug") or posting_identity.board_scope(source, url),
            external_id=str(data["source_job_id"]) if data.get("source_job_id") is not None else None,
            url=url, first_seen_at=now, last_seen_at=now)
        db.add(listing)
    previous_seen = parse_time(listing.last_seen_at)
    listing.job_id = job.id
    listing.last_seen_at = max(previous_seen, now) if previous_seen else now
    job.last_seen_at = max(parse_time(job.last_seen_at), now) if job.last_seen_at else now
    updated = parse_time(data.get("updated_at"))
    older = bool((previous_seen and now < previous_seen)
                 or (listing.closed_at and now < parse_time(listing.closed_at))
                 or (updated and listing.upstream_updated_at
                 and updated < parse_time(listing.upstream_updated_at)))
    if older:
        data["_stale_revision"] = True
        return []
    listing.closed_at = None
    if source in posting_identity.AUTHORITATIVE_SOURCES and (
            data.get("_listed_open") or job.closed_note == "no longer listed on its board") and (
            not job.closed_at or now >= parse_time(job.closed_at)):
        job.closed_at = None
        job.closed_note = None

    previous = dict(listing.snapshot or {})
    snapshot = {**previous, **{key: data[key] for key in _FIELDS if data.get(key) is not None}}
    pay_fields = {"salary_min", "salary_max", "salary_currency", "salary_period"}
    stated_pay = data.get("salary_min") is not None or data.get("salary_max") is not None
    if stated_pay:
        snapshot.update({field: data.get(field) for field in pay_fields})
    description = clean(data.get("description") or "")
    if description:
        snapshot["description_hash"] = hashlib.sha256(description.encode()).hexdigest()
        snapshot["description_length"] = len(description)
        listing.details_checked_at = now
    digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True, default=str).encode()).hexdigest()
    if updated:
        listing.upstream_updated_at = updated
    improved = []
    authoritative = source in posting_identity.AUTHORITATIVE_SOURCES
    if authoritative:
        protect_pay = any(is_manual(job, name) for name in pay_fields)
        if stated_pay and not protect_pay:
            from app.services.job_details import normalise_period
            # Keep the employer's band and unit together; never combine a new
            # minimum with a stale maximum or currency from another source.
            for field in pay_fields:
                incoming = normalise_period(data.get(field)) if field == "salary_period" else data.get(field)
                if getattr(job, field) != incoming:
                    setattr(job, field, incoming)
                    improved.append(field)
        # Missing fields are silence, not evidence that the employer removed a
        # requirement. Explicit changed values (including False/zero) count.
        for field in _FIELDS:
            if field not in data or data[field] is None or is_manual(job, field):
                continue
            if field in pay_fields:
                continue  # Pay was handled as one stated unit above.
            incoming = data[field]
            if field in ("title", "company", "location") and not incoming:
                continue
            if getattr(job, field) != incoming:
                setattr(job, field, incoming)
                improved.append(field)
        # A complete description from the employer can become shorter. Empty
        # detail failures must never erase text. Normalized identical text is
        # unchanged regardless of HTML formatting.
        if description and not is_manual(job, "description") and description != clean(job.description or ""):
            job.description = description
            improved.append("description")
        if any(f in improved for f in ("salary_min", "salary_max", "salary_currency", "salary_period")):
            from app.services.job_details import annualise
            job.salary_annual_min = annualise(job.salary_min, job.salary_period, job.salary_currency)
            job.salary_annual_max = annualise(job.salary_max, job.salary_period, job.salary_currency)
        if improved:
            if any(f in improved for f in ("company", "title", "location")):
                from app.services.deduplication import compute_dedupe_hash
                job.dedupe_hash = compute_dedupe_hash(job.company, job.title, job.location or "", job.url)
            # Application IDs/status stay intact; generated documents can use
            # this stamp to surface that their evidence changed.
            job.description_updated_at = now
            job.details_extracted_at = None
            if not job.dismissed_at and not job.applications:
                job.status = JobStatus.new
                job.filter_reason = job.filter_detail = None
    if digest != listing.content_hash:
        listing.content_hash = digest
        listing.snapshot = snapshot
        db.flush()
        db.add(ListingRevision(listing_key=key, observed_at=now, content_hash=digest, snapshot=snapshot))
        db.flush()
        keep = max(1, int(live().COLLECTION_REVISION_HISTORY))
        old_ids = [r[0] for r in db.query(ListingRevision.id).filter(ListingRevision.listing_key == key)
                   .order_by(ListingRevision.observed_at.desc(), ListingRevision.id.desc()).offset(keep)]
        if old_ids:
            db.query(ListingRevision).filter(ListingRevision.id.in_(old_ids)).delete(synchronize_session=False)
    return improved
