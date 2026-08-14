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

    def test_it_records_where_the_link_landed(self, db):
        job = make_job(db)
        self._completed(db, {"final_url": EMPLOYER, "html": ""})
        task = db.query(BrowserTask).filter(BrowserTask.kind == "resolve_link").one()
        note = task.result["ingest"]
        assert note["jobs_updated"] == 1
        assert EMPLOYER[:40] in note["landed_on"]

    def test_it_records_when_a_real_page_load_was_needed(self, db):
        # Worth seeing before every link starts needing a window: it means the
        # aggregator has started refusing background requests outright.
        make_job(db)
        self._completed(db, {"final_url": EMPLOYER, "html": "", "via": "tab"})
        task = db.query(BrowserTask).filter(BrowserTask.kind == "resolve_link").one()
        assert task.result["ingest"]["via"] == "tab"

    def test_an_ordinary_fetch_is_recorded_as_such(self, db):
        make_job(db)
        self._completed(db, {"final_url": EMPLOYER, "html": ""})
        task = db.query(BrowserTask).filter(BrowserTask.kind == "resolve_link").one()
        assert task.result["ingest"]["via"] == "fetch"

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


class TestRedditViaBrowser:
    """
    The queue doing what it was built for.

    Reddit answers a datacenter IP with 403 Blocked — a categorical refusal, not
    a rate limit, so no retry from the server ever succeeds. The browser is not
    blocked because it is a browser on a home connection, which is the entire
    premise.
    """

    def test_queues_one_task_per_subreddit(self, db):
        from app.services.interview_sources import reddit_search_urls

        queued = agent_work.enqueue_reddit_search(db, "Amazon")
        assert queued == len(reddit_search_urls("Amazon"))
        tasks = db.query(BrowserTask).filter(BrowserTask.kind == "fetch_json").all()
        assert len(tasks) == queued
        assert all(t.payload["purpose"] == "interview_reddit" for t in tasks)

    def test_it_outranks_background_link_resolution(self, db):
        # Somebody pressed a button and is waiting; link resolution is tidying.
        agent_work.enqueue_reddit_search(db, "Amazon")
        task = db.query(BrowserTask).filter(BrowserTask.kind == "fetch_json").first()
        assert task.priority > 0

    def test_an_empty_company_queues_nothing(self, db):
        assert agent_work.enqueue_reddit_search(db, "  ") == 0

    def _complete_with(self, db, result, company="Amazon"):
        """Report on whichever task was actually leased, not the first row.

        Leasing takes one task by its own ordering, which need not be the row a
        separate query returns first — reporting on the wrong id fails with
        "not leased" and looks like a bug in completion.
        """
        agent_work.enqueue_reddit_search(db, company)
        leased = browser_tasks.lease(db, ["fetch_json"], agent_id="ext-1")
        assert leased, "nothing was leased"
        return browser_tasks.complete(db, leased[0].id, result, agent_id="ext-1")

    def _reddit_payload(self, created_days_ago=20):
        from datetime import datetime, timedelta, timezone

        created = (datetime.now(timezone.utc) - timedelta(days=created_days_ago)).timestamp()
        return {"data": {"children": [{"data": {
            "title": "Amazon interview experience — SDE-1",
            "selftext": "Three rounds: OA, phone screen, onsite. " * 5,
            "created_utc": created,
            "permalink": "/r/leetcode/comments/xyz/amazon/",
        }}]}}

    def test_what_the_browser_fetched_lands_in_the_corpus(self, db):
        from app.models.interview_report import InterviewReport

        self._complete_with(db, {"status": 200, "json": self._reddit_payload()})
        report = db.query(InterviewReport).one()
        assert report.source == "reddit"
        assert report.company_key == "amazon"

    def test_the_same_parser_runs_as_on_the_direct_path(self, db):
        # Only the thing that made the request differed, so a post that the
        # direct path would reject must be rejected here too.
        from app.models.interview_report import InterviewReport

        payload = {"data": {"children": [{"data": {
            "title": "How should I prepare for Amazon?",
            "selftext": "Any tips?",
            "created_utc": 1750000000,
            "permalink": "/r/leetcode/comments/q/",
        }}]}}
        self._complete_with(db, {"status": 200, "json": payload})
        assert db.query(InterviewReport).count() == 0

    def test_a_non_json_response_is_harmless(self, db):
        from app.models.interview_report import InterviewReport

        done = self._complete_with(db, {"status": 200, "json": None, "text": "<html>"})
        assert done.status == "done"
        assert db.query(InterviewReport).count() == 0

    def test_it_records_what_ingestion_made_of_the_result(self, db):
        # The failure this prevents: a task that succeeded and yielded nothing
        # read exactly like one that never ran, which is what "it did nothing"
        # turned out to mean the first time this ran for real.
        done = self._complete_with(db, {"status": 200, "json": self._reddit_payload()})
        note = (done.result or {}).get("ingest")
        assert note is not None
        assert note["posts_seen"] == 1
        assert note["kept"] == 1
        assert note["stored"] == 1

    def test_it_distinguishes_nothing_found_from_nothing_kept(self, db):
        payload = {"data": {"children": [{"data": {
            "title": "How should I prepare for Amazon?",
            "selftext": "Any tips?",
            "created_utc": 1750000000,
            "permalink": "/r/leetcode/comments/q/",
        }}]}}
        done = self._complete_with(db, {"status": 200, "json": payload})
        note = done.result["ingest"]
        assert note["posts_seen"] == 1, "the search did return something"
        assert note["kept"] == 0, "and the filter is what dropped it"

    def test_the_raw_body_is_not_kept_after_ingestion(self, db):
        # It has served its purpose and a search response is large.
        done = self._complete_with(db, {"status": 200, "json": self._reddit_payload()})
        assert "json" not in (done.result or {})

    def test_a_non_json_result_says_so(self, db):
        done = self._complete_with(db, {"status": 200, "json": None, "text": "<html>"})
        assert "did not get JSON" in done.result["ingest"]["error"]

    def test_an_unknown_purpose_is_ignored(self, db):
        task = browser_tasks.enqueue(
            db, "fetch_json", {"url": "https://x/1", "purpose": "something_else"}
        )
        browser_tasks.lease(db, ["fetch_json"], agent_id="ext-1")
        done = browser_tasks.complete(db, task.id, {"json": {"a": 1}}, agent_id="ext-1")
        assert done.status == "done"
