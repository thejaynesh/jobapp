"""
Queueing link resolution to the browser, and ingesting what comes back.

The behaviours worth defending are the ones that keep the queue honest over
weeks rather than one cycle: not re-queueing the same dead link forever, not
overwriting an apply URL the server already found, and not letting an ingestion
bug destroy work an agent actually did.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.models.browser_task import BrowserTask
from app.models.job import Job
from app.models.profile import Profile
from app.services import agent_work, browser_tasks

INTERSTITIAL = "https://www.adzuna.com/land/ad/123456"
EMPLOYER = "https://boards.greenhouse.io/acme/jobs/4001"


def make_job(db, url=INTERSTITIAL, apply_url=None, **kwargs):
    job = Job(
        source=kwargs.pop("source", "adzuna"),
        url=url,
        apply_url=apply_url,
        title=kwargs.pop("title", "Backend Engineer"),
        company=kwargs.pop("company", "Acme"),
        location=kwargs.pop("location", "Boston, MA"),
        description=kwargs.pop("description", "A job."),
        dedupe_hash=kwargs.pop("dedupe_hash", f"hash-{url}"),
        fetched_at=kwargs.pop("fetched_at", datetime.now(timezone.utc)),
        **kwargs,
    )
    db.add(job)
    db.commit()
    return job


class TestEnqueue:
    def test_queues_an_unresolved_interstitial(self, db):
        make_job(db)
        assert agent_work.enqueue_unresolved_links(db) == 1
        task = db.query(BrowserTask).one()
        assert task.kind == "resolve_link"
        assert task.payload["url"] == INTERSTITIAL

    def test_ignores_jobs_that_already_have_an_apply_url(self, db):
        make_job(db, apply_url=EMPLOYER)
        assert agent_work.enqueue_unresolved_links(db) == 0

    def test_ignores_direct_employer_links(self, db):
        make_job(db, url=EMPLOYER, source="greenhouse", dedupe_hash="h2")
        assert agent_work.enqueue_unresolved_links(db) == 0

    def test_queues_one_task_for_a_url_many_jobs_share(self, db):
        make_job(db, dedupe_hash="h1")
        make_job(db, dedupe_hash="h2", title="Frontend Engineer")
        assert agent_work.enqueue_unresolved_links(db) == 1

    def test_does_not_requeue_work_already_in_flight(self, db):
        make_job(db)
        agent_work.enqueue_unresolved_links(db)
        assert agent_work.enqueue_unresolved_links(db) == 0

    def test_does_not_requeue_a_link_tried_recently(self, db):
        # A link that failed will fail again; retrying every cycle would crowd
        # out work that might succeed.
        make_job(db)
        agent_work.enqueue_unresolved_links(db)
        task = db.query(BrowserTask).one()
        task.status = "failed"
        db.commit()
        assert agent_work.enqueue_unresolved_links(db) == 0

    def test_retries_after_the_cooldown(self, db):
        make_job(db)
        agent_work.enqueue_unresolved_links(db)
        task = db.query(BrowserTask).one()
        task.status = "failed"
        task.created_at = datetime.now(timezone.utc) - timedelta(days=30)
        db.commit()
        assert agent_work.enqueue_unresolved_links(db) == 1

    def test_respects_the_budget(self, db, monkeypatch):
        monkeypatch.setattr(settings, "AGENT_LINK_RESOLVE_MAX_QUEUED", 2)
        for n in range(5):
            make_job(db, url=f"https://www.adzuna.com/land/ad/{n}", dedupe_hash=f"h{n}")
        assert agent_work.enqueue_unresolved_links(db) == 2

    def test_a_zero_budget_queues_nothing(self, db, monkeypatch):
        monkeypatch.setattr(settings, "AGENT_LINK_RESOLVE_MAX_QUEUED", 0)
        make_job(db)
        assert agent_work.enqueue_unresolved_links(db) == 0

    def test_an_empty_database_is_not_an_error(self, db):
        assert agent_work.enqueue_unresolved_links(db) == 0


class TestIngest:
    def _completed(self, db, result, url=INTERSTITIAL):
        task = browser_tasks.enqueue(db, "resolve_link", {"url": url})
        browser_tasks.lease(db, agent_id="ext-1")
        return browser_tasks.complete(db, task.id, result, agent_id="ext-1")

    def test_stores_the_apply_url_the_browser_found(self, db):
        job = make_job(db)
        self._completed(db, {"final_url": EMPLOYER, "html": ""})
        db.refresh(job)
        assert job.apply_url == EMPLOYER

    def test_updates_every_job_sharing_that_url(self, db):
        a = make_job(db, dedupe_hash="h1")
        b = make_job(db, dedupe_hash="h2", title="Frontend Engineer")
        self._completed(db, {"final_url": EMPLOYER, "html": ""})
        db.refresh(a)
        db.refresh(b)
        assert a.apply_url == b.apply_url == EMPLOYER

    def test_does_not_overwrite_an_apply_url_already_found(self, db):
        existing = "https://jobs.lever.co/acme/already-known"
        job = make_job(db, apply_url=existing)
        self._completed(db, {"final_url": EMPLOYER, "html": ""})
        db.refresh(job)
        assert job.apply_url == existing

    def test_landing_on_another_aggregator_is_not_an_apply_link(self, db):
        job = make_job(db)
        self._completed(db, {"final_url": "https://www.indeed.com/viewjob?jk=9", "html": ""})
        db.refresh(job)
        assert job.apply_url is None

    def test_a_landing_page_still_yields_ats_slugs(self, db):
        # The whole reason the HTML comes back even when the redirect dead-ends:
        # the page names the company's board even when it is not an apply link.
        db.add(Profile(data={}))
        db.commit()
        make_job(db)
        self._completed(db, {
            "final_url": "https://www.indeed.com/viewjob?jk=9",
            "html": '<a href="https://boards.greenhouse.io/stripe/jobs/1">Apply</a>',
        })
        discovered = db.query(Profile).first().data.get("discovered_ats") or {}
        assert "stripe" in discovered.get("greenhouse", [])

    def test_an_unresolvable_link_changes_nothing(self, db):
        job = make_job(db)
        self._completed(db, {"final_url": "", "html": ""})
        db.refresh(job)
        assert job.apply_url is None

    def test_a_result_for_an_unknown_url_is_harmless(self, db):
        make_job(db)
        self._completed(db, {"final_url": EMPLOYER}, url="https://www.adzuna.com/land/ad/999")
        assert db.query(Job).one().apply_url is None

    def test_ping_results_are_stored_without_ingestion(self, db):
        task = browser_tasks.enqueue(db, "ping")
        browser_tasks.lease(db, agent_id="ext-1")
        done = browser_tasks.complete(db, task.id, {"pong": True}, agent_id="ext-1")
        assert done.status == "done"
        assert done.result == {"pong": True}


class TestIngestFailureIsContained:
    def test_a_broken_handler_does_not_lose_the_result(self, db, monkeypatch):
        # The agent did the work and reported it honestly. An ingestion bug is
        # ours, and must not be charged to the task.
        def explode(db, task):
            raise RuntimeError("handler is broken")

        monkeypatch.setitem(agent_work.RESULT_HANDLERS, "resolve_link", explode)
        task = browser_tasks.enqueue(db, "resolve_link", {"url": INTERSTITIAL})
        browser_tasks.lease(db, agent_id="ext-1")
        done = browser_tasks.complete(db, task.id, {"final_url": EMPLOYER}, agent_id="ext-1")

        assert done.status == "done"
        assert done.result == {"final_url": EMPLOYER}

    def test_ingest_never_raises(self, db, monkeypatch):
        def explode(db, task):
            raise RuntimeError("nope")

        monkeypatch.setitem(agent_work.RESULT_HANDLERS, "ping", explode)
        task = browser_tasks.enqueue(db, "ping")
        agent_work.ingest(db, task)  # must not raise
