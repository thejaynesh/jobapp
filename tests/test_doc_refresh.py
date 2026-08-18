"""
Rewriting the documents that were written from a thinner posting.

Enrichment routinely replaces an aggregator's 500-character teaser with the
real posting. Anything generated before that arrived was tailored to
requirements nobody had read yet — and the badge that said so put the work on
the user: notice it, click Rewrite, once per application.

The interesting half of this feature is what it refuses to touch.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.models.application import (
    Application, ApplicationDocument, ApplicationStatus, DocType,
)
from app.models.job import Job, JobStatus
from app.services import doc_refresh

NOW = datetime.now(timezone.utc)


def _job(**kwargs) -> Job:
    defaults = dict(
        source="greenhouse",
        source_urls=[f"https://x/{uuid.uuid4()}"],
        title="Backend Engineer",
        company="Acme",
        url=f"https://x/{uuid.uuid4()}",
        description="The real posting. " * 50,
        status=JobStatus.docs_generated,
        fetched_at=NOW - timedelta(days=5),
        dedupe_hash=uuid.uuid4().hex,
        description_updated_at=NOW - timedelta(hours=1),
    )
    defaults.update(kwargs)
    return Job(**defaults)


def _setup(db, *, written_at=None, job_kwargs=None, doc_kwargs=None, **app_kwargs):
    """One application with one current document of each type."""
    job = _job(**(job_kwargs or {}))
    db.add(job)
    db.flush()
    application = Application(job_id=job.id, **app_kwargs)
    db.add(application)
    db.flush()
    written_at = written_at if written_at is not None else NOW - timedelta(days=2)
    fields = {"is_current": True, **(doc_kwargs or {})}
    for doc_type in (DocType.resume, DocType.cover_letter):
        db.add(ApplicationDocument(
            application_id=application.id, doc_type=doc_type, version=1,
            path=f"/tmp/{uuid.uuid4()}.pdf", created_at=written_at, **fields,
        ))
    db.commit()
    db.refresh(application)
    return application


@pytest.fixture
def queued():
    with patch("app.tasks.generate.queue_generation", return_value=True) as mock:
        yield mock


class TestWhatItPicksUp:
    def test_documents_older_than_the_description_are_stale(self, db):
        application = _setup(db)
        assert application.documents_are_stale is True
        assert doc_refresh.stale_applications(db) == [application]

    def test_documents_written_after_it_are_not(self, db):
        application = _setup(db, written_at=NOW - timedelta(minutes=5))
        assert application.documents_are_stale is False
        assert doc_refresh.stale_applications(db) == []

    def test_a_job_whose_description_never_grew_is_not(self, db):
        application = _setup(db, job_kwargs={"description_updated_at": None})
        assert application.documents_are_stale is False
        assert doc_refresh.stale_applications(db) == []

    def test_the_freshest_evidence_comes_first(self, db):
        older = _setup(db, job_kwargs={
            "description_updated_at": NOW - timedelta(days=1)})
        newer = _setup(db, job_kwargs={
            "description_updated_at": NOW - timedelta(minutes=10)})

        assert doc_refresh.stale_applications(db) == [newer, older]

    def test_it_queues_a_rewrite(self, db, queued):
        application = _setup(db)

        result = doc_refresh.refresh_stale_documents(db)

        assert result["queued"] == 1
        queued.assert_called_once_with(application.id, feedback=None)


class TestWhatItRefusesToTouch:
    @pytest.mark.parametrize("status", [
        ApplicationStatus.applied,
        ApplicationStatus.interviewing,
        ApplicationStatus.offered,
        ApplicationStatus.rejected,
        ApplicationStatus.withdrawn,
    ])
    def test_anything_the_user_acted_on(self, db, status):
        # The file on disk is the record of what the employer received.
        # Replacing it destroys the only evidence of what was claimed.
        _setup(db, status=status)
        assert doc_refresh.stale_applications(db) == []

    def test_a_generation_already_in_flight(self, db):
        _setup(db, generation_status="generating")
        assert doc_refresh.stale_applications(db) == []

    def test_a_failed_generation(self, db):
        # It has an error the user can read and a button to retry; re-queueing
        # it on a timer burns calls on the same failure.
        _setup(db, generation_status="failed")
        assert doc_refresh.stale_applications(db) == []

    def test_an_application_with_no_current_documents(self, db):
        # That is `sweep_generations`' job, and two sweepers queueing one
        # application is two workers writing to the same rows.
        application = _setup(db, doc_kwargs={"is_current": False})
        assert application.documents_are_stale is False
        assert doc_refresh.stale_applications(db) == []

    def test_nothing_at_all_when_it_is_switched_off(self, db, queued, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "DOC_REFRESH_ENABLED", False)
        _setup(db)

        result = doc_refresh.refresh_stale_documents(db)

        assert result == {"eligible": 0, "queued": 0, "skipped": 0, "enabled": False}
        queued.assert_not_called()


class TestItKeepsWhatTheUserAskedFor:
    def test_the_last_rewrite_instruction_is_carried_forward(self, db, queued):
        # Otherwise a refresh nobody asked for quietly undoes a rewrite the
        # user did ask for, and reads as the system overruling them.
        application = _setup(db, doc_kwargs={
            "generation_feedback": "Lead with the Kafka work."})

        doc_refresh.refresh_stale_documents(db)

        queued.assert_called_once_with(
            application.id, feedback="Lead with the Kafka work."
        )

    def test_the_most_recent_instruction_wins(self, db):
        application = _setup(db, doc_kwargs={"generation_feedback": "Old note."})
        db.add(ApplicationDocument(
            application_id=application.id, doc_type=DocType.resume, version=2,
            path="/tmp/v2.pdf", is_current=True,
            created_at=NOW - timedelta(days=1),
            generation_feedback="Newer note.",
        ))
        db.commit()
        db.refresh(application)

        assert doc_refresh.carried_feedback(application) == "Newer note."


class TestItDoesNotQueueTheSameThingTwice:
    def test_a_queued_application_is_marked_in_flight(self, db, queued):
        # An 'idle' row is re-selected by the next pass until a worker picks
        # the first copy up — one stale application becomes a pile of tasks.
        application = _setup(db)

        doc_refresh.refresh_stale_documents(db)

        db.refresh(application)
        assert application.generation_status == "generating"
        assert application.generation_started_at is not None
        assert doc_refresh.stale_applications(db) == []

    def test_a_broker_failure_leaves_the_row_alone(self, db):
        # Nothing was queued, so nothing should look in flight — the next pass
        # has to be able to try again.
        application = _setup(db)

        with patch("app.tasks.generate.queue_generation", return_value=False):
            result = doc_refresh.refresh_stale_documents(db)

        db.refresh(application)
        assert result["queued"] == 0
        assert result["skipped"] == 1
        assert application.generation_status == "idle"


class TestItStaysBounded:
    def test_a_pass_is_capped(self, db, queued, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "DOC_REFRESH_MAX_PER_RUN", 2)
        for _ in range(5):
            _setup(db)

        result = doc_refresh.refresh_stale_documents(db)

        assert result["queued"] == 2
        assert queued.call_count == 2


class TestTheBadgeAndTheSweepAgree:
    def test_the_page_says_a_rewrite_is_coming(self, client, db):
        _setup(db)
        application = doc_refresh.stale_applications(db)[0]

        body = client.get(f"/apps/{application.id}").text

        assert "became fuller after these documents were generated" in body
        assert "queued automatically" in body

    def test_the_page_says_sent_documents_are_left_alone(self, client, db):
        application = _setup(db, status=ApplicationStatus.applied)

        body = client.get(f"/apps/{application.id}").text

        assert "became fuller after these documents were generated" in body
        assert "left alone" in body
