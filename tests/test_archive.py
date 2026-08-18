"""
Moving long-dead jobs out of the hot table, without forgetting them.

The `jobs` table is mostly descriptions, and most of those belong to postings
rejected months ago. But the fact that we have *seen* a posting is not
disposable: deduplication has three layers, and deleting a job defeats all
three silently — the next fetch re-inserts it as new, spends a scoring call,
reaches the same verdict, and does it again next week.

So the tests that matter here are the dedupe ones, and the three carve-outs
for jobs that must never be archived at all.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.models.application import Application, ApplicationStatus
from app.models.archived_job import ArchivedJob
from app.models.job import Job, JobStatus
from app.services import archive
from app.services.deduplication import (
    compute_dedupe_hash, find_existing_job, was_archived,
)

OLD = datetime.now(timezone.utc) - timedelta(days=200)
RECENT = datetime.now(timezone.utc) - timedelta(days=3)


def _job(db, *, fetched_at=OLD, status=JobStatus.filtered_out,
         filter_reason="low_score", url=None, source_job_id="req-1",
         company="Acme", title="Backend Engineer", location="Remote",
         **kwargs) -> Job:
    url = url or f"https://boards.example/{uuid.uuid4()}"
    job = Job(
        source="greenhouse", source_job_id=source_job_id, source_urls=[url],
        title=title, company=company, location=location, url=url,
        description="A long description. " * 50,
        status=status, filter_reason=filter_reason, fetched_at=fetched_at,
        dedupe_hash=compute_dedupe_hash(company, title, location), **kwargs,
    )
    db.add(job)
    db.commit()
    return job


class TestWhatItMoves:
    def test_an_old_rejection_goes(self, db):
        job = _job(db)
        url, dedupe = job.url, job.dedupe_hash

        result = archive.archive(db)

        assert result["archived"] == 1
        assert db.query(Job).count() == 0
        row = db.query(ArchivedJob).one()
        assert row.url == url
        assert row.dedupe_hash == dedupe
        assert row.filter_reason == "low_score"

    def test_the_original_id_is_carried_over(self, db):
        # Anything still holding a reference — the LLM log keeps job_id as a
        # plain column on purpose — can at least be traced to the tombstone.
        job = _job(db)
        job_id = job.id
        archive.archive(db)
        assert db.query(ArchivedJob).one().id == job_id

    def test_a_recent_rejection_stays(self, db):
        _job(db, fetched_at=RECENT)
        assert archive.archive(db)["archived"] == 0
        assert db.query(Job).count() == 1

    def test_the_window_is_configurable(self, db):
        _job(db, fetched_at=datetime.now(timezone.utc) - timedelta(days=10))
        assert archive.archive(db, days=90)["archived"] == 0
        assert archive.archive(db, days=5)["archived"] == 1

    def test_a_pass_is_bounded(self, db, monkeypatch):
        # The first run has a six-figure backlog, and one transaction that size
        # holds a worker and a lock for its duration.
        monkeypatch.setattr(settings, "ARCHIVE_MAX_PER_RUN", 2)
        for n in range(5):
            _job(db, title=f"Engineer {n}")

        result = archive.archive(db)

        assert result["archived"] == 2
        assert result["remaining"] == 3

    def test_the_oldest_go_first(self, db):
        newer = _job(db, title="Newer", fetched_at=OLD)
        _job(db, title="Older", fetched_at=OLD - timedelta(days=100))

        archive.archive(db, limit=1)

        assert db.query(ArchivedJob).one().title == "Older"
        assert db.query(Job).one().id == newer.id

    def test_it_does_nothing_when_switched_off(self, db, monkeypatch):
        monkeypatch.setattr(settings, "ARCHIVE_ENABLED", False)
        _job(db)
        assert archive.archive(db)["enabled"] is False
        assert db.query(Job).count() == 1


class TestWhatItRefusesToTouch:
    @pytest.mark.parametrize("status", [
        JobStatus.new, JobStatus.matched, JobStatus.docs_generated,
    ])
    def test_anything_not_settled(self, db, status):
        _job(db, status=status, filter_reason=None)
        assert archive.archive(db)["archived"] == 0

    def test_anything_with_an_application(self, db):
        # That is the user's pipeline, and the row is attached to documents on
        # disk.
        job = _job(db)
        db.add(Application(job_id=job.id, status=ApplicationStatus.applied))
        db.commit()

        assert archive.archive(db)["archived"] == 0
        assert db.query(Job).count() == 1

    @pytest.mark.parametrize("reason", sorted(archive.PROTECTED_REASONS))
    def test_a_verdict_the_user_made(self, db, reason):
        # These are the labels `match_eval` builds its fixture from, and it
        # needs the description. Archiving them would quietly destroy the only
        # ground truth this system has about its own scoring.
        _job(db, filter_reason=reason)

        assert archive.archive(db)["archived"] == 0
        assert db.query(Job).count() == 1

    def test_the_protected_set_matches_what_the_harness_reads(self, db):
        # If these ever drift apart, archiving silently eats the fixture.
        from app.services.match_eval import _USER_REJECTIONS

        assert set(_USER_REJECTIONS) == archive.PROTECTED_REASONS


class TestDeduplicationStillWorks:
    """The whole reason this moves rather than deletes."""

    def test_an_archived_url_is_recognised(self, db):
        job = _job(db)
        url, source_job_id, dedupe = job.url, job.source_job_id, job.dedupe_hash
        archive.archive(db)

        assert find_existing_job(db, "greenhouse", url, None, "no-such-hash") is None
        assert was_archived(db, "greenhouse", url, source_job_id, dedupe) is True

    def test_an_archived_source_id_is_recognised(self, db):
        # Layer 2: the same posting under a URL that picked up a new tracking
        # parameter.
        job = _job(db, source_job_id="req-9999")
        archive.archive(db)

        assert was_archived(
            db, "greenhouse", "https://boards.example/moved", "req-9999",
            "unrelated-hash",
        ) is True

    def test_an_archived_content_hash_is_recognised(self, db):
        # Layer 3: a cross-post from a different board entirely.
        _job(db, company="Acme", title="Backend Engineer", location="Remote")
        archive.archive(db)
        # The cosmetic variations the hash is normalized to absorb: a company
        # suffix, and a work-mode tag bolted onto the title.
        same = compute_dedupe_hash("Acme, Inc.", "Backend Engineer - Remote",
                                   "Remote")

        assert was_archived(db, "lever", "https://elsewhere/1", "other-id",
                            same) is True

    def test_an_unrelated_posting_is_not(self, db):
        _job(db)
        archive.archive(db)

        assert was_archived(
            db, "lever", "https://elsewhere/1", "other-id",
            compute_dedupe_hash("Globex", "Data Scientist", "Berlin"),
        ) is False

    def test_the_harvest_skips_an_archived_posting(self, db):
        # End to end. Without this the archive is worse than useless: every
        # archived posting still on its board returns as new next cycle, costs
        # a scoring call, and is archived again sixty days later.
        from app.services.harvest import save_harvested_jobs

        job = _job(db)
        url = job.url
        archive.archive(db)

        counts = save_harvested_jobs(db, [{
            "source_job_id": "req-1", "url": url, "title": "Backend Engineer",
            "company": "Acme", "location": "Remote",
            "description": "A description.",
        }])

        assert counts["inserted"] == 0
        assert db.query(Job).count() == 0

    def test_a_genuinely_new_posting_still_gets_in(self, db):
        # The check must not turn into a wall.
        from app.services.harvest import save_harvested_jobs

        _job(db)
        archive.archive(db)

        counts = save_harvested_jobs(db, [{
            "source_job_id": "new-1", "url": "https://jobs.lever.co/globex/1",
            "title": "Data Scientist", "company": "Globex",
            "location": "Berlin", "description": "A description.",
        }])

        assert counts["inserted"] == 1

    def test_the_fetcher_guards_the_same_way(self, db):
        # The fetcher's insert loop is inline in `fetch_and_save_jobs`, so it
        # is checked here by construction rather than driven end to end: both
        # paths call the one guard with the same four arguments, and a future
        # edit that drops it from one of them fails this.
        import inspect

        from app.services import harvest, job_fetcher

        for module in (job_fetcher, harvest):
            source = inspect.getsource(module)
            assert "was_archived(db, source, url, source_job_id, dedupe_hash)" \
                in source, module.__name__


class TestATombstoneThatAlreadyExists:
    def test_the_live_row_goes_without_a_second_tombstone(self, db):
        # `dedupe_hash` is unique on both tables, so an insert that collided
        # would fail the constraint and roll back the whole batch — losing
        # every job in the pass, not just the awkward one.
        #
        # It should not be reachable now that the fetcher checks the archive
        # before inserting, but it *was* reachable for anything stored before
        # that check existed, and a batch-losing crash is a poor way to find
        # out.
        job = _job(db)
        db.add(ArchivedJob(
            id=uuid.uuid4(), source="lever", source_job_id="older",
            source_urls=["https://x/older"], url="https://x/older",
            dedupe_hash=job.dedupe_hash, title="Backend Engineer",
            company="Acme", location="Remote", filter_reason="low_score",
            fetched_at=OLD,
        ))
        db.commit()

        result = archive.archive(db)

        assert result["archived"] == 0
        assert result["skipped"] == 1
        assert db.query(Job).count() == 0
        assert db.query(ArchivedJob).count() == 1


class TestReporting:
    def test_the_funnel_still_counts_archived_rejections(self, db):
        # Otherwise the day this first runs, a hundred thousand jobs appear
        # never to have been filtered at all.
        from app.services import funnel

        _job(db, filter_reason="low_score")
        _job(db, title="Another", filter_reason="title_mismatch")
        archive.archive(db)

        result = funnel.overview(db)

        assert result["archived"] == 2
        assert result["total"] == 2
        reasons = {row["reason"]: row["count"] for row in result["filter_reasons"]}
        assert reasons == {"low_score": 1, "title_mismatch": 1}

    def test_live_and_archived_reasons_are_added_together(self, db):
        from app.services import funnel

        _job(db, filter_reason="low_score")
        archive.archive(db)
        _job(db, title="Fresh", filter_reason="low_score", fetched_at=RECENT)

        reasons = {
            row["reason"]: row["count"]
            for row in funnel.overview(db)["filter_reasons"]
        }
        assert reasons["low_score"] == 2

    def test_the_page_names_the_archive(self, client, db):
        _job(db)
        archive.archive(db)

        body = client.get("/funnel").text
        assert "archived" in body.lower()

    def test_status_reports_what_is_left(self, db):
        _job(db)
        _job(db, title="Recent", fetched_at=RECENT)

        state = archive.status(db)
        assert state["eligible"] == 1
        assert state["total"] == 0

        archive.archive(db)
        state = archive.status(db)
        assert state["total"] == 1
        assert state["eligible"] == 0
