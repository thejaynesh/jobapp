import pytest
from datetime import datetime, timezone
from app.models.job import Job, JobStatus
from app.models.application import Application, ApplicationDocument, ApplicationStatus, DocType


def _make_job(db, suffix="1"):
    job = Job(
        source="adzuna",
        title="SWE",
        company="Acme",
        url=f"https://example.com/job/{suffix}",
        description="...",
        fetched_at=datetime.now(timezone.utc),
        dedupe_hash=f"hash{suffix}",
        status=JobStatus.docs_generated,
    )
    db.add(job)
    db.flush()
    return job


def test_create_application(db):
    job = _make_job(db, "a1")
    app = Application(job_id=job.id)
    db.add(app)
    db.flush()

    assert app.id is not None
    assert app.status == ApplicationStatus.not_applied
    assert app.outreach_contacts == []
    assert app.applied_at is None


def test_application_status_transition(db):
    job = _make_job(db, "a2")
    app = Application(job_id=job.id)
    db.add(app)
    db.flush()

    app.status = ApplicationStatus.applied
    app.applied_at = datetime.now(timezone.utc)
    db.flush()

    fetched = db.query(Application).filter_by(id=app.id).first()
    assert fetched.status == ApplicationStatus.applied
    assert fetched.applied_at is not None


def test_create_application_document(db):
    job = _make_job(db, "a3")
    app = Application(job_id=job.id)
    db.add(app)
    db.flush()

    doc = ApplicationDocument(
        application_id=app.id,
        doc_type=DocType.resume,
        version=1,
        path="/storage/resumes/resume_acme_swe_20260611_v1.pdf",
        is_current=True,
    )
    db.add(doc)
    db.flush()

    assert doc.id is not None
    assert doc.generation_feedback is None


def test_document_version_history(db):
    job = _make_job(db, "a4")
    app = Application(job_id=job.id)
    db.add(app)
    db.flush()

    v1 = ApplicationDocument(
        application_id=app.id,
        doc_type=DocType.resume,
        version=1,
        path="/storage/resumes/v1.pdf",
        is_current=False,
    )
    v2 = ApplicationDocument(
        application_id=app.id,
        doc_type=DocType.resume,
        version=2,
        path="/storage/resumes/v2.pdf",
        generation_feedback="Too formal",
        is_current=True,
    )
    db.add_all([v1, v2])
    db.flush()

    docs = (
        db.query(ApplicationDocument)
        .filter_by(application_id=app.id, doc_type=DocType.resume)
        .order_by(ApplicationDocument.version)
        .all()
    )
    assert len(docs) == 2
    assert docs[0].is_current is False
    assert docs[1].generation_feedback == "Too formal"
