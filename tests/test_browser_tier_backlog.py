"""
The walled-off half of enrichment, and four ways it was doing nothing.

The panel that exposed this showed sixteen passes inside one minute, each
reporting `tried 0` and `browser 200`, with a hundred and ten thousand jobs
still thin and a note saying the browser harvest had never produced anything.
Every number was real; none of the work was.

What was wrong, in the order it bites:

1. A job handed to the browser was never stamped as attempted, so
   `select_targets` — which is newest-first and skips recently attempted rows —
   picked the same two hundred every pass, forever.
2. `queue_for_browser` checked nothing before enqueueing, so those two hundred
   became two hundred more tasks each time.
3. Chaining counted queued browser work as work done. Handing URLs to a queue
   takes a second, so a batch of nothing but walled-off hosts chained instantly
   and burned all fifty passes in under a minute.
4. And underneath all of it, the tasks were the wrong kind: `resolve_link`
   fetches without cookies, so twelve thousand LinkedIn jobs were being fetched
   as a stranger and getting a sign-in wall.

Plus a fifth that would have wasted the fix: a description arriving by harvest
set the "it grew" stamp but never put the job back in the matching queue, so a
job filtered as `no_description` would have kept that verdict after getting one.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.models.browser_task import BrowserTask
from app.models.job import Job, JobStatus
from app.services import enrichment

LINKEDIN = "https://www.linkedin.com/jobs/view/{n}/"


def make_job(db, n=1, **overrides):
    fields = {
        "source": "linkedin",
        "source_urls": [LINKEDIN.format(n=n)],
        "title": "Backend Engineer",
        "company": f"Acme {n}",
        "url": LINKEDIN.format(n=n),
        "source_job_id": str(4000000000 + n),
        "description": None,
        "status": JobStatus.new,
        "fetched_at": datetime.now(timezone.utc),
        "dedupe_hash": uuid.uuid4().hex,
    }
    fields.update(overrides)
    job = Job(**fields)
    db.add(job)
    db.commit()
    return job


def tasks(db):
    return db.query(BrowserTask).all()


class TestHandingAJobToTheBrowserCountsAsAnAttempt:
    def test_a_queued_job_is_stamped(self, db):
        # Nothing else about the row changes when the work is delegated, so
        # without this it stays at the head of a newest-first ordering.
        job = make_job(db)
        enrichment.enrich_jobs(db, [job])

        assert job.enrichment_attempted_at is not None

    def test_the_next_pass_reaches_different_jobs(self, db):
        # The bug as the panel showed it: pass after pass on the same two
        # hundred rows while the backlog behind them was never touched.
        for n in range(4):
            make_job(db, n=n)

        first = enrichment.select_targets(db, limit=2)
        enrichment.enrich_jobs(db, first)
        second = enrichment.select_targets(db, limit=2)

        assert {job.id for job in first}.isdisjoint({job.id for job in second})

    def test_a_job_the_queue_had_no_room_for_is_not_stamped(self, db, monkeypatch):
        # It was not attempted. Marking it would put it to sleep for a week
        # over a moment of congestion.
        monkeypatch.setattr(settings, "ENRICH_MAX_BROWSER_OUTSTANDING", 1)
        first, second = make_job(db, n=1), make_job(db, n=2)

        enrichment.enrich_jobs(db, [first, second])

        assert first.enrichment_attempted_at is not None
        assert second.enrichment_attempted_at is None


class TestItDoesNotQueueTheSameUrlTwice:
    def test_a_url_already_waiting_is_skipped(self, db):
        job = make_job(db)
        enrichment.queue_for_browser(db, [job])
        enrichment.queue_for_browser(db, [job])

        assert len(tasks(db)) == 1

    def test_a_finished_task_does_not_block_a_later_retry(self, db):
        # Only queued and leased work counts as outstanding. A job whose visit
        # failed a fortnight ago should be tried again.
        job = make_job(db)
        enrichment.queue_for_browser(db, [job])
        db.query(BrowserTask).one().status = "failed"
        db.commit()

        enrichment.queue_for_browser(db, [job])
        assert len(tasks(db)) == 2

    def test_the_outstanding_queue_is_capped(self, db, monkeypatch):
        # A browser drains this at human pace. Queueing faster does not make
        # anything arrive sooner; it builds a backlog that expires unread.
        monkeypatch.setattr(settings, "ENRICH_MAX_BROWSER_OUTSTANDING", 3)
        jobs = [make_job(db, n=n) for n in range(10)]

        assert enrichment.queue_for_browser(db, jobs) == 3

    def test_a_full_queue_adds_nothing(self, db, monkeypatch):
        monkeypatch.setattr(settings, "ENRICH_MAX_BROWSER_OUTSTANDING", 2)
        enrichment.queue_for_browser(db, [make_job(db, n=1), make_job(db, n=2)])

        assert enrichment.queue_for_browser(db, [make_job(db, n=3)]) == 0


class TestTheRightKindOfBrowserWork:
    def test_linkedin_is_browsed_rather_than_fetched(self, db):
        # `resolve_link` fetches without cookies — deliberately, since resolving
        # a public redirect has no business carrying the user's sessions. For
        # LinkedIn that means asking as a stranger and getting a sign-in wall,
        # which is why queueing twelve thousand produced nothing at all.
        enrichment.queue_for_browser(db, [make_job(db)])

        assert db.query(BrowserTask).one().kind == "browse_page"

    def test_a_browse_carries_the_pace(self, db):
        enrichment.queue_for_browser(db, [make_job(db)])

        payload = db.query(BrowserTask).one().payload
        assert payload["gap_seconds"] >= 5
        assert payload["settle_seconds"] >= 1

    def test_a_host_the_harvest_does_not_cover_is_still_fetched(self, db):
        # No interceptor on that page, so opening a tab would buy nothing that
        # a fetch does not.
        job = make_job(db, url="https://www.example-board.com/jobs/1",
                       source="adzuna", source_job_id=None)
        enrichment.queue_for_browser(db, [job])

        assert db.query(BrowserTask).one().kind == "resolve_link"

    def test_the_job_it_was_queued_for_travels_with_it(self, db):
        job = make_job(db)
        enrichment.queue_for_browser(db, [job])

        assert db.query(BrowserTask).one().payload["job_id"] == str(job.id)


class TestChainingCountsWorkDoneNotWorkDelegated:
    def _chain(self, **result):
        from app.tasks.enrich import _chain_if_more

        from unittest.mock import patch

        with patch("app.tasks.enrich.enrich_jobs.delay") as delay:
            _chain_if_more({"attempted": 0, "queued_browser": 0, **result},
                           200, False, 0)
        return delay.called

    def test_a_pass_that_only_queued_browser_work_does_not_chain(self, db):
        # The panel's sixteen passes in one minute. Handing URLs to a queue is
        # a second's work, and the thing it queues is drained by a browser at
        # human pace, which no amount of chaining speeds up.
        assert self._chain(attempted=0, queued_browser=200) is False

    def test_a_full_batch_of_real_fetches_still_chains(self, db):
        # The guard must not turn chaining off; the backlog is the reason it
        # exists.
        assert self._chain(attempted=200, queued_browser=0) is True

    def test_a_partial_batch_still_ends_the_chain(self, db):
        assert self._chain(attempted=12, queued_browser=200) is False


class TestAHarvestedDescriptionGetsTheJobScoredAgain:
    """
    The fifth bug, and the one that would have wasted fixing the other four:
    a description arriving by harvest set the "it grew" stamp and stopped there.
    """

    def _harvest(self, db, job, description):
        from app.services.harvest import save_harvested_jobs

        return save_harvested_jobs(db, [{
            "source": job.source, "source_job_id": job.source_job_id,
            "url": job.url, "title": job.title, "company": job.company,
            "location": job.location or "", "description": description,
        }])

    def test_a_job_filtered_for_no_description_goes_back_in_the_queue(self, db):
        job = make_job(db, status=JobStatus.filtered_out,
                       filter_reason="no_description",
                       filter_detail="The posting had no description.")
        self._harvest(db, job, "The real posting text. " * 60)
        db.refresh(job)

        assert job.status == JobStatus.new
        assert job.filter_reason is None
        assert job.filter_detail is None

    def test_a_verdict_the_user_made_is_left_alone(self, db):
        job = make_job(db, status=JobStatus.filtered_out, filter_reason="manual")
        self._harvest(db, job, "The real posting text. " * 60)
        db.refresh(job)

        assert job.status == JobStatus.filtered_out

    def test_a_job_with_an_application_is_left_alone(self, db):
        from app.models.application import Application

        job = make_job(db, status=JobStatus.filtered_out,
                       filter_reason="no_description")
        db.add(Application(job_id=job.id))
        db.commit()

        self._harvest(db, job, "The real posting text. " * 60)
        db.refresh(job)

        assert job.status == JobStatus.filtered_out

    def test_a_trivial_change_does_not_requeue(self, db):
        job = make_job(db, description="x" * 400,
                       status=JobStatus.filtered_out, filter_reason="low_score")
        self._harvest(db, job, "x" * 420)
        db.refresh(job)

        assert job.status == JobStatus.filtered_out

    def test_a_cross_post_merge_requeues_too(self, db):
        # Same rule, same reason: this is what "the description got fuller"
        # means, whichever path filled it in.
        from app.services.deduplication import merge_or_skip

        job = make_job(db, status=JobStatus.filtered_out,
                       filter_reason="no_description")
        merge_or_skip(db, job, "https://elsewhere/1",
                      "The real posting text. " * 60, layer=3)

        assert job.status == JobStatus.new
