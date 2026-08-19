"""
Enrichment that keeps going instead of idling for half an hour.

A 200-job batch takes under a minute, so the schedule alone spent 29 of every
30 minutes doing nothing while a six-figure backlog waited. Chaining fixes
that — but only safely once a pass remembers what it already tried.

Without that stamp, `select_targets` picks the newest thin jobs, and a posting
whose description cannot be improved is unchanged by the attempt: it stays at
the head of the queue and is picked again by every pass, forever. Chaining
would have turned a slow waste into a hot loop against the same wall.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.config import settings
from app.models.job import Job, JobStatus
from app.services import enrichment, enrichment_history

NOW = datetime.now(timezone.utc)


def _job(db, *, attempted_at=None, description="short", **kwargs):
    job = Job(
        source="greenhouse", source_urls=[f"https://x/{uuid.uuid4()}"],
        title="Backend Engineer", company="Acme",
        url=f"https://x/{uuid.uuid4()}", description=description,
        status=JobStatus.new, fetched_at=NOW,
        dedupe_hash=uuid.uuid4().hex,
        enrichment_attempted_at=attempted_at, **kwargs,
    )
    db.add(job)
    db.commit()
    return job


class TestItRemembersWhatItTried:
    def test_a_job_never_tried_is_offered(self, db):
        job = _job(db)
        assert [j.id for j in enrichment.select_targets(db)] == [job.id]

    def test_a_job_just_tried_is_not_offered_again(self, db):
        # The whole point. Nothing about a failed attempt changes the job, so
        # without this it sits at the head of a newest-first queue forever and
        # the real backlog behind it is never reached.
        _job(db, attempted_at=NOW - timedelta(hours=1))
        assert enrichment.select_targets(db) == []

    def test_a_job_tried_long_ago_is_offered_again(self, db):
        # A cooloff, not a write-off: a host refusing us last week may not be
        # refusing us next week.
        job = _job(db, attempted_at=NOW - timedelta(days=30))
        assert [j.id for j in enrichment.select_targets(db)] == [job.id]

    def test_the_window_is_configurable(self, db, monkeypatch):
        _job(db, attempted_at=NOW - timedelta(days=3))
        monkeypatch.setattr(settings, "ENRICH_RETRY_DAYS", 7)
        assert enrichment.select_targets(db) == []
        monkeypatch.setattr(settings, "ENRICH_RETRY_DAYS", 1)
        assert len(enrichment.select_targets(db)) == 1

    def test_an_attempt_that_found_nothing_is_still_stamped(self, db):
        # The failure case is exactly the one that must be recorded — a success
        # changes the description and drops out of the queue by itself.
        job = _job(db)

        with patch.object(enrichment, "_browser_only", return_value=False), \
             patch("httpx.Client"), \
             patch.object(enrichment, "extract_from_html", return_value=None):
            enrichment.enrich_jobs(db, [job], queue_browser=False)

        db.refresh(job)
        assert job.enrichment_attempted_at is not None
        assert enrichment.select_targets(db) == []

    def test_the_backlog_separates_waiting_from_thin(self, db):
        # A panel showing only "thin" would read as a backlog that refuses to
        # drain, when most of it is simply cooling off.
        _job(db)
        _job(db, attempted_at=NOW - timedelta(hours=2))

        counts = enrichment_history.backlog(db)
        assert counts["thin"] == 2
        assert counts["waiting"] == 1


class TestChaining:
    def _run(self, result, limit=None, depth=0, monkeypatch=None):
        from app.tasks import enrich

        with patch.object(enrich.enrich_jobs, "delay") as queued:
            enrich._chain_if_more(result, limit, True, depth)
        return queued

    def test_a_full_batch_queues_the_next_one(self, db, monkeypatch):
        monkeypatch.setattr(settings, "ENRICH_CHAIN_PASSES", True)
        monkeypatch.setattr(settings, "ENRICH_MAX_PER_RUN", 200)

        queued = self._run({"attempted": 200, "queued_browser": 0})

        queued.assert_called_once()
        assert queued.call_args.kwargs["depth"] == 1

    def test_browser_queued_jobs_do_not_count_towards_a_full_batch(self, db,
                                                                    monkeypatch):
        # They were handed to a queue, not done. This test asserted the
        # opposite until a run showed what it cost: a batch made entirely of
        # walled-off hosts takes about a second, so it chained instantly and
        # burned all fifty passes inside a minute — sixteen of them visible on
        # the panel with the same timestamp, each queueing the same two hundred
        # URLs again.
        #
        # And chaining could not have helped even if it were free: browser work
        # is drained by a person's browser at a person's pace, which no amount
        # of queueing ahead speeds up.
        monkeypatch.setattr(settings, "ENRICH_CHAIN_PASSES", True)
        monkeypatch.setattr(settings, "ENRICH_MAX_PER_RUN", 200)

        self._run({"attempted": 50, "queued_browser": 150}).assert_not_called()

    def test_a_full_batch_of_real_fetches_still_chains_past_browser_work(
        self, db, monkeypatch,
    ):
        # The guard must not turn chaining off: the backlog is why it exists.
        monkeypatch.setattr(settings, "ENRICH_CHAIN_PASSES", True)
        monkeypatch.setattr(settings, "ENRICH_MAX_PER_RUN", 200)

        self._run({"attempted": 200, "queued_browser": 150}).assert_called_once()

    def test_an_unfull_batch_ends_the_chain(self, db, monkeypatch):
        # Nothing left to do, so the schedule is the right place for "a few new
        # jobs arrived" rather than another immediate pass.
        monkeypatch.setattr(settings, "ENRICH_CHAIN_PASSES", True)
        monkeypatch.setattr(settings, "ENRICH_MAX_PER_RUN", 200)

        self._run({"attempted": 12, "queued_browser": 0}).assert_not_called()

    def test_a_skipped_pass_does_not_chain(self, db, monkeypatch):
        # It never ran. Chaining off a lock collision would spin.
        monkeypatch.setattr(settings, "ENRICH_CHAIN_PASSES", True)
        self._run({"skipped_reason": "already running"}).assert_not_called()

    def test_a_crashed_pass_does_not_chain(self, db, monkeypatch):
        monkeypatch.setattr(settings, "ENRICH_CHAIN_PASSES", True)
        self._run(None).assert_not_called()

    def test_the_chain_is_capped(self, db, monkeypatch):
        monkeypatch.setattr(settings, "ENRICH_CHAIN_PASSES", True)
        monkeypatch.setattr(settings, "ENRICH_MAX_PER_RUN", 200)
        monkeypatch.setattr(settings, "ENRICH_MAX_CHAINED_PASSES", 5)

        full = {"attempted": 200, "queued_browser": 0}
        self._run(full, depth=3).assert_called_once()
        self._run(full, depth=4).assert_not_called()

    def test_it_can_be_switched_off(self, db, monkeypatch):
        monkeypatch.setattr(settings, "ENRICH_CHAIN_PASSES", False)
        monkeypatch.setattr(settings, "ENRICH_MAX_PER_RUN", 200)

        self._run({"attempted": 200, "queued_browser": 0}).assert_not_called()

    def test_an_explicit_limit_sets_the_bar(self, db, monkeypatch):
        # A manual pass of 50 that returned 50 is full; the default of 200 is
        # not the ceiling that applies.
        monkeypatch.setattr(settings, "ENRICH_CHAIN_PASSES", True)
        monkeypatch.setattr(settings, "ENRICH_MAX_PER_RUN", 200)

        self._run({"attempted": 50, "queued_browser": 0}, limit=50).assert_called_once()
