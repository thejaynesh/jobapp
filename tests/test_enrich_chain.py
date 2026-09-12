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

        # `refresh` because this is about the counting, not the cache.
        counts = enrichment_history.backlog(db, refresh=True)
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


class TestTheBacklogCountIsNotFreeToAsk:
    """
    Counting the thin-description backlog is a parallel sequential scan that
    de-TOASTs every description on the table. Measured on a live box: 117
    seconds, five gigabytes of reads, 300,941 rows — and *three of them running
    at once*, each repeating the others' work because three requests arrived
    while the first was still going. Refreshing the panel was a denial of
    service.

    It is not a missing index. 55% of the table matches the predicate, so no
    index is selective enough for the planner to prefer one, and the partial
    index that already exists (`ix_jobs_enrichment_targets`) is correctly
    ignored. On that selectivity a sequential scan really is cheaper; the only
    thing to fix is how often it is paid for.
    """

    def _counts(self, n=3):
        return {"thin": n, "waiting": 1, "rescuable": 0}

    def test_a_cached_answer_is_used_instead_of_counting(self, db, monkeypatch):
        monkeypatch.setattr(enrichment_history, "_cached_backlog",
                            lambda: self._counts(999))

        def explode(*a, **k):
            raise AssertionError("counted despite a warm cache")

        monkeypatch.setattr(enrichment_history, "_waiting", explode)
        assert enrichment_history.backlog(db)["thin"] == 999

    def test_refresh_pays_for_the_count_deliberately(self, db, monkeypatch):
        monkeypatch.setattr(enrichment_history, "_cached_backlog",
                            lambda: self._counts(999))
        _job(db)
        assert enrichment_history.backlog(db, refresh=True)["thin"] == 1

    def test_a_fresh_count_is_stored_for_the_next_caller(self, db, monkeypatch):
        stored = {}
        monkeypatch.setattr(enrichment_history, "_cached_backlog", lambda: None)
        monkeypatch.setattr(enrichment_history, "_store_backlog", stored.update)
        _job(db)

        assert enrichment_history.backlog(db)["thin"] == 1
        assert stored["thin"] == 1

    def test_a_failed_count_is_never_cached(self, db, monkeypatch):
        """
        Zeros are the failure, not the answer. Caching them would show an empty
        backlog for ten minutes after one bad query — which reads as "all done"
        rather than as "something went wrong".
        """
        monkeypatch.setattr(enrichment_history, "_cached_backlog", lambda: None)

        def refuse(*a, **k):
            raise RuntimeError("no")

        monkeypatch.setattr(enrichment_history, "_waiting", refuse)

        def must_not_store(*a, **k):
            raise AssertionError("cached a failure")

        monkeypatch.setattr(enrichment_history, "_store_backlog", must_not_store)
        assert enrichment_history.backlog(db) == {
            "thin": 0, "waiting": 0, "rescuable": 0}

    def test_an_unreachable_cache_costs_nothing_but_the_cache(self, db, monkeypatch):
        """Redis down is not a reason for the panel to lose its numbers."""
        def unreachable(*a, **k):
            raise OSError("connection refused")

        monkeypatch.setattr(enrichment_history, "_cached_backlog", unreachable)
        _job(db)
        # The helper swallows its own errors, so reaching the count at all is
        # the assertion; this pins that `backlog` does not propagate them.
        try:
            counts = enrichment_history.backlog(db, refresh=True)
        except OSError:
            raise AssertionError("a dead cache took the panel down with it")
        assert counts["thin"] == 1


class TestTheRequeueSweepDoesNotScanTheWholeTable:
    """
    The first version of `requeue_settled_verdicts` selected on
    `func.length(Job.description) >= 1500`, which is a parallel sequential scan
    that de-TOASTs every description on the table — the identical 117-second
    query that had just been cached out of the panel one commit earlier, now
    paid on every enrichment pass instead.

    Nothing can index it. The partial index that exists covers the thin side of
    the comparison, and `length()` on a Text column has no other route. So the
    narrowing has to happen on `status` and `filter_reason`, which are indexed,
    and the length has to be tested on the rows that come back.
    """

    def test_the_length_test_never_reaches_sql(self, db):
        """
        Asserted against the SQL actually issued, not the source. The first
        attempt at this test read the function text and tripped over the word
        `func.length` in the comment explaining why it must not be there.
        """
        from sqlalchemy import event

        statements = []

        def record(conn, cursor, statement, params, context, many):
            statements.append(statement)

        engine = db.get_bind()
        event.listen(engine, "before_cursor_execute", record)
        try:
            enrichment.requeue_settled_verdicts(db)
        finally:
            event.remove(engine, "before_cursor_execute", record)

        assert statements, "issued no SQL at all"
        offenders = [s for s in statements if "length(" in s.lower()]
        assert not offenders, (
            "a length() comparison scans and de-TOASTs the whole table: "
            + offenders[0][:200]
        )

    def test_it_still_only_takes_jobs_with_a_real_description(self, db):
        from app.models.job import Job, JobStatus

        for chars in (100, 5000, 200, 6000):
            job = _job(db)
            job.status = JobStatus.filtered_out
            job.filter_reason = "few_skills"
            job.description = "x" * chars
        db.commit()

        assert enrichment.requeue_settled_verdicts(db) == 2
        back = db.query(Job).filter(Job.status == JobStatus.new).all()
        assert sorted(len(j.description) for j in back) == [5000, 6000]

    def test_the_limit_counts_what_moved_not_what_was_read(self, db):
        """
        Over-fetching is how the length test gets done at all, so the ceiling
        has to apply to jobs actually requeued — otherwise a batch of mostly
        short descriptions would return almost nothing and the backlog would
        crawl.
        """
        from app.models.job import JobStatus

        for i in range(10):
            job = _job(db)
            job.status = JobStatus.filtered_out
            job.filter_reason = "low_score"
            # Every other one too short to qualify.
            job.description = "x" * (5000 if i % 2 else 100)
        db.commit()

        assert enrichment.requeue_settled_verdicts(db, limit=3) == 3
        assert enrichment.requeue_settled_verdicts(db, limit=99) == 2
