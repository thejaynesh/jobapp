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


def test_job_dedupe_hash_unique(db):
    from sqlalchemy.exc import IntegrityError

    job1 = Job(
        source="indeed",
        title="SWE",
        company="Corp",
        url="https://example.com/1",
        description="...",
        fetched_at=datetime.now(timezone.utc),
        dedupe_hash="samehash",
    )
    job2 = Job(
        source="linkedin",
        title="SWE",
        company="Corp",
        url="https://example.com/2",
        description="...",
        fetched_at=datetime.now(timezone.utc),
        dedupe_hash="samehash",
    )
    db.add(job1)
    db.flush()
    db.add(job2)

    with pytest.raises(IntegrityError):
        db.flush()
