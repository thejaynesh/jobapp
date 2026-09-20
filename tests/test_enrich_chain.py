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

    def test_no_length_comparison_runs_against_the_whole_table(self, db):
        """
        Asserted against the SQL actually issued, not the source. The first
        attempt at this test read the function text and tripped over the word
        `func.length` in the comment explaining why it must not be there.

        The rule is not "never call length()" — it is "never let length()
        decide which rows to look at". A `length()` in the outer WHERE is the
        117-second parallel scan this class is named for. The same call inside
        a correlated EXISTS is evaluated only on rows the indexed `status` and
        `filter_reason` predicates already selected, which is what makes the
        already-judged exclusion affordable: measured against 120k rows with a
        60k-row backlog, an index scan and an anti-join, 167ms.
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
        offenders = [
            s for s in statements
            if "length(" in s.lower() and "exists" not in s.lower()
        ]
        assert not offenders, (
            "a length() comparison outside a correlated subquery scans and "
            "de-TOASTs the whole table: " + offenders[0][:200]
        )

    def test_the_narrowing_predicates_are_still_the_indexed_ones(self, db):
        """The length test is only affordable because these run first."""
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

        select = next((s for s in statements if "FROM jobs" in s), "")
        assert "status" in select and "filter_reason" in select

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


class TestAVerdictIsNotRevisitedOnEvidenceItAlreadySaw:
    """
    The re-queue was a treadmill.

    A job rejected on a *complete* description satisfies every condition
    `requeue_settled_verdicts` selects on, so it went back to `new`, was
    re-scored, was rejected again for the same reason on the same text, and
    was picked up by the next pass thirty minutes later. Measured on a stub
    matcher before the fix: five passes, five scoring calls, one unchanged
    job — and a `new` queue that never empties, which is what actually hurts,
    because a posting fetched this morning waits behind a re-run of June.

    `JobScore.description_chars` is what closes it: it records how much text
    the model saw, so "has the evidence changed?" is answerable without
    guessing.
    """

    def _settled(self, db, *, chars, judged_on=None, reason="low_score"):
        from app.models.job_score import JobScore

        job = _job(db, description="x" * chars)
        job.status = JobStatus.filtered_out
        job.filter_reason = reason
        if judged_on is not None:
            db.add(JobScore(job_id=job.id, description_chars=judged_on,
                            status="filtered_out", filter_reason=reason))
        db.commit()
        db.refresh(job)
        return job

    def test_a_job_judged_on_the_text_it_holds_is_left_alone(self, db):
        self._settled(db, chars=5000, judged_on=5000)
        assert enrichment.requeue_settled_verdicts(db) == 0

    def test_it_stays_left_alone_pass_after_pass(self, db):
        """The loop: without the guard this returned 1 every time, forever."""
        self._settled(db, chars=5000, judged_on=5000)
        assert [enrichment.requeue_settled_verdicts(db) for _ in range(5)] == [0] * 5

    def test_a_description_that_actually_grew_still_comes_back(self, db):
        """The feature this whole module exists for must survive the fix."""
        job = self._settled(db, chars=6000, judged_on=500)
        assert enrichment.requeue_settled_verdicts(db) == 1
        db.refresh(job)
        assert job.status == JobStatus.new
        assert job.filter_reason is None

    def test_a_job_never_scored_gets_its_one_catch_up_pass(self, db):
        """
        Rows predating the score history (migration 0026) carry no evaluation
        at all. They deserve one look; after it they hold a row like everything
        else and settle.
        """
        self._settled(db, chars=5000, judged_on=None)
        assert enrichment.requeue_settled_verdicts(db) == 1

    def test_whitespace_is_not_new_evidence(self, db):
        """Below the bar every other writer in this codebase uses."""
        self._settled(db, chars=5000, judged_on=5000 - 40)
        assert enrichment.requeue_settled_verdicts(db) == 0

    def test_the_bar_is_the_same_one_the_writers_use(self, db):
        self._settled(db, chars=5000,
                      judged_on=5000 - enrichment.MIN_IMPROVEMENT_CHARS)
        assert enrichment.requeue_settled_verdicts(db) == 1

    def test_the_rule_has_one_definition(self, db):
        """
        `_worth_rescoring` is what the enrichment path calls too, so a
        description that grew by an enrichment pass and one that grew by a
        cross-post merge get the same answer.
        """
        settled = self._settled(db, chars=5000, judged_on=5000)
        grown = self._settled(db, chars=5000, judged_on=200)
        assert enrichment._worth_rescoring(settled) is False
        assert enrichment._worth_rescoring(grown) is True

    def test_an_applied_job_is_still_never_revisited(self, db):
        """The older guard has to survive the new one."""
        from app.models.application import Application

        job = self._settled(db, chars=6000, judged_on=200)
        db.add(Application(job_id=job.id))
        db.commit()
        db.refresh(job)
        assert enrichment._worth_rescoring(job) is False


class TestTheServerPassRemembersWhichHostsAnswer:
    """
    The browser path has always honoured a paused or challenge-blocked host.
    `for_server` honoured nothing at all, so a host that has never once
    produced a description was asked again every seven days for as long as the
    backlog existed. Jooble: 2,788 attempts, 13 descriptions, and the other
    2,775 repeated indefinitely.

    A rate, not a count, and that is the whole of it. Adzuna threw 86 failures
    in a single pass and is the most productive source in the table at 51% — it
    leads the failure column because it leads the attempt column. Any rule
    reading `failures_by_host` alone would have switched off the best source in
    the system, which is why the denominator had to be recorded first.
    """

    def _runs(self, db, host, attempts, successes, days_ago=0):
        from datetime import timedelta

        from app.models.enrichment_run import EnrichmentRun

        db.add(EnrichmentRun(
            started_at=NOW - timedelta(days=days_ago),
            host_outcomes={host: {"a": attempts, "s": successes}},
        ))
        db.commit()

    def test_a_host_that_gives_nothing_is_dropped(self, db, monkeypatch):
        monkeypatch.setattr(settings, "ENRICH_HOST_MIN_ATTEMPTS", 50)
        self._runs(db, "jooble.org", attempts=2788, successes=13)
        assert "jooble.org" in enrichment.unproductive_hosts(db)

    def test_the_most_productive_source_is_not(self, db, monkeypatch):
        """
        Adzuna fails more than anything else because it tries more than
        anything else. The rule has to see past that or it turns off half the
        database.
        """
        monkeypatch.setattr(settings, "ENRICH_HOST_MIN_ATTEMPTS", 50)
        self._runs(db, "www.adzuna.com", attempts=88053, successes=45185)
        assert enrichment.unproductive_hosts(db) == set()

    def test_a_host_with_too_few_attempts_is_left_alone(self, db, monkeypatch):
        """Nought out of three is not evidence of anything."""
        monkeypatch.setattr(settings, "ENRICH_HOST_MIN_ATTEMPTS", 50)
        self._runs(db, "new.example", attempts=3, successes=0)
        assert enrichment.unproductive_hosts(db) == set()

    def test_old_evidence_ages_out_so_the_host_is_retried(self, db, monkeypatch):
        """
        What makes this self-healing rather than a permanent ban. A skipped
        host records no new attempts, so its evidence leaves the window and it
        is tried again — a host that fixed itself recovers unaided.
        """
        monkeypatch.setattr(settings, "ENRICH_HOST_MIN_ATTEMPTS", 50)
        monkeypatch.setattr(settings, "ENRICH_HOST_MEMORY_DAYS", 14)
        self._runs(db, "jooble.org", attempts=2788, successes=13, days_ago=30)
        assert enrichment.unproductive_hosts(db) == set()

    def test_evidence_adds_up_across_runs(self, db, monkeypatch):
        monkeypatch.setattr(settings, "ENRICH_HOST_MIN_ATTEMPTS", 50)
        for _ in range(6):
            self._runs(db, "quiet.example", attempts=10, successes=0)
        assert "quiet.example" in enrichment.unproductive_hosts(db)

    def test_a_subdomain_counts_as_the_host(self, db, monkeypatch):
        monkeypatch.setattr(settings, "ENRICH_HOST_MIN_ATTEMPTS", 50)
        self._runs(db, "jooble.org", attempts=200, successes=0)
        skipped = enrichment.unproductive_hosts(db)
        assert enrichment._host_is_unproductive("https://uk.jooble.org/x", skipped)
        assert not enrichment._host_is_unproductive(
            "https://notjooble.org/x", skipped)

    def test_a_pass_skips_those_jobs_and_still_stamps_them(self, db, monkeypatch):
        """
        Stamped, or they sit at the head of a newest-first ordering and starve
        the hosts that do work — the failure `enrichment_attempted_at` exists
        for, and one this codebase has already hit once.
        """
        monkeypatch.setattr(settings, "ENRICH_HOST_MIN_ATTEMPTS", 50)
        self._runs(db, "quiet.example", attempts=200, successes=0)
        job = _job(db)
        job.url = "https://quiet.example/jobs/1"
        job.source_urls = [job.url]
        db.commit()

        stats = enrichment.enrich_jobs(db, [job])
        db.refresh(job)
        assert stats.attempted == 0, "no request was made"
        assert stats.skipped_unproductive == 1
        assert job.enrichment_attempted_at is not None

    def test_a_pass_records_the_denominator(self, db, monkeypatch):
        """Without attempts there is no rate, only a count of failures."""
        from unittest.mock import patch

        job = _job(db)
        job.url = "https://fresh.example/jobs/1"
        job.source_urls = [job.url]
        db.commit()

        with patch.object(enrichment, "enrich_one",
                          side_effect=RuntimeError("refused")):
            stats = enrichment.enrich_jobs(db, [job])
        assert stats.host_outcomes["fresh.example"] == {"a": 1, "s": 0}

    def test_an_unreadable_history_costs_nothing(self, db, monkeypatch):
        """Skipping nothing is the old behaviour: wasteful, never wrong."""
        def boom(*a, **k):
            raise RuntimeError("no")

        monkeypatch.setattr(db, "query", boom)
        assert enrichment.unproductive_hosts(db) == set()
