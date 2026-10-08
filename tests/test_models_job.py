import pytest
from datetime import datetime, timezone
from app.models.job import Job, JobStatus


def test_create_job(db):
    job = Job(
        source="adzuna",
        title="Software Engineer",
        company="Stripe",
        location="New York",
        url="https://example.com/job/123",
        description="We are looking for a SWE...",
        fetched_at=datetime.now(timezone.utc),
        dedupe_hash="abc123",
    )
    db.add(job)
    db.flush()

    assert job.id is not None
    assert job.status == JobStatus.new
    assert job.is_remote is False
    assert job.source_urls == []
    assert job.matched_skills == []
    assert job.missing_skills == []


def test_job_status_enum(db):
    job = Job(
        source="linkedin",
        title="Backend Engineer",
        company="Acme",
        url="https://example.com/job/456",
        description="...",
        fetched_at=datetime.now(timezone.utc),
        dedupe_hash="def456",
        status=JobStatus.matched,
    )
    db.add(job)
    db.flush()

    fetched = db.query(Job).filter_by(id=job.id).first()
    assert fetched.status == JobStatus.matched


def test_distinct_postings_can_share_a_content_fingerprint(db):
    job1 = Job(
        source="indeed",
        title="SWE",
        company="Corp",
        url="https://example.com/1",
        description="...",
        fetched_at=datetime.now(timezone.utc),
        dedupe_hash="samehash",
    )
    db.add(job1)
    db.flush()

    job2 = Job(
        source="linkedin",
        title="SWE",
        company="Corp",
        url="https://example.com/2",
        description="...",
        fetched_at=datetime.now(timezone.utc),
        dedupe_hash="samehash",
    )
    db.add(job2)
    db.flush()
    assert job1.id != job2.id
    assert db.query(Job).filter_by(dedupe_hash="samehash").count() == 2


def test_exact_posting_identity_is_unique(db):
    from sqlalchemy.exc import IntegrityError

    values = dict(source="indeed", title="SWE", company="Corp",
                  fetched_at=datetime.now(timezone.utc), dedupe_hash="samehash",
                  identity_key="a" * 64)
    db.add(Job(url="https://example.com/1", **values))
    db.flush()

    with pytest.raises(IntegrityError):
        with db.begin_nested():
            db.add(Job(url="https://example.com/2", **values))
            db.flush()
