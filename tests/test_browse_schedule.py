"""
Keeping the browser's queue topped up without a button being pressed.

The crawl was manual only, which made it something the user remembers rather
than something the system does — and the whole argument for browsing is that it
reaches sources no API can. A source that runs only when someone thinks of it
is not really a source.

But it is a backstop, not a second fetch cycle, and every test here is about
that distinction. The queue drains at a person's pace — one page every twenty
seconds — so the useful question is never "should we fetch more" but "has the
browser run out of work". Getting that wrong in either direction is bad in a
specific way: too eager and the queue fills with tasks that expire before
anyone opens them, burying the real backlog; too shy and the browser sits idle
between button presses, which is what this exists to fix.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.models.browser_task import BrowserTask
from app.models.job import Job, JobStatus
from app.models.profile import Profile
from app.services import browse_plan

PROFILE = {"target_roles": ["Backend Engineer"], "target_locations": ["London"]}


def thin_job(db, n=0):
    job = Job(
        source="linkedin",
        source_urls=[f"https://x/{uuid.uuid4()}"],
        title="Backend Engineer", company=f"Acme {n}",
        url=f"https://www.linkedin.com/jobs/view/40123456{n:02d}/",
        source_job_id=f"40123456{n:02d}",
        description="thin", status=JobStatus.new,
        fetched_at=datetime.now(timezone.utc), dedupe_hash=uuid.uuid4().hex,
    )
    db.add(job)
    db.commit()
    return job


def agent_polled(db, minutes_ago=1):
    """A leased task is the simplest proof a browser asked for work."""
    task = BrowserTask(
        kind="ping", payload={}, status="done",
        leased_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db.add(task)
    db.commit()
    return task


def waiting(db):
    return (
        db.query(BrowserTask)
        .filter(BrowserTask.kind == "browse_page",
                BrowserTask.status.in_(("queued", "leased")))
        .count()
    )


class TestItRunsWithoutABeingAsked:
    def test_an_empty_queue_gets_topped_up(self, db):
        agent_polled(db)
        thin_job(db)

        assert browse_plan.scheduled_crawl(db, PROFILE)["queued"] > 0
        assert waiting(db) > 0

    def test_it_serves_the_description_backlog_first(self, db):
        # A posting already stored with no description is worth more than one
        # nobody has seen: it has been scored on a fragment, and the fragment
        # is why.
        agent_polled(db)
        thin_job(db)

        outcome = browse_plan.scheduled_crawl(db, PROFILE)
        assert outcome["kind"] == "postings"

    def test_it_searches_once_the_backlog_is_drained(self, db):
        agent_polled(db)

        outcome = browse_plan.scheduled_crawl(db, PROFILE)
        assert outcome["kind"] == "searches"
        assert outcome["queued"] > 0


class TestItDoesNotOutrunTheBrowser:
    def test_a_queue_still_draining_is_left_alone(self, db, monkeypatch):
        # One page every twenty seconds. Refilling a working queue would
        # outrun the browser by an order of magnitude.
        monkeypatch.setattr(settings, "BROWSE_TOPUP_BELOW", 10)
        agent_polled(db)
        for n in range(30):
            thin_job(db, n)
        browse_plan.crawl_postings(db)
        before = waiting(db)

        outcome = browse_plan.scheduled_crawl(db, PROFILE)

        assert outcome["queued"] == 0
        assert "draining" in outcome["skipped"]
        assert waiting(db) == before

    def test_a_nearly_empty_queue_is_topped_up(self, db, monkeypatch):
        monkeypatch.setattr(settings, "BROWSE_TOPUP_BELOW", 10)
        agent_polled(db)
        thin_job(db, 1)
        browse_plan.crawl_postings(db, limit=1)

        assert browse_plan.scheduled_crawl(db, PROFILE)["queued"] > 0


class TestItWillNotQueueForABrowserThatIsNotThere:
    def test_nothing_is_queued_when_no_agent_has_polled(self, db):
        # Work queued for a shut laptop expires unread and hides the real
        # backlog behind it — and unlike a fetch, the server cannot run it.
        thin_job(db)

        outcome = browse_plan.scheduled_crawl(db, PROFILE)
        assert outcome["queued"] == 0
        assert outcome["skipped"] == "no agent"
        assert waiting(db) == 0

    def test_an_agent_that_stopped_days_ago_does_not_count(self, db):
        agent_polled(db, minutes_ago=60 * 24 * 3)
        thin_job(db)

        assert browse_plan.scheduled_crawl(db, PROFILE)["queued"] == 0

    def test_a_recent_poll_is_enough(self, db):
        agent_polled(db, minutes_ago=30)
        assert browse_plan.agent_seen_recently(db) is True

    def test_the_window_is_configurable(self, db, monkeypatch):
        agent_polled(db, minutes_ago=120)
        monkeypatch.setattr(settings, "BROWSE_AGENT_STALE_HOURS", 1)

        assert browse_plan.agent_seen_recently(db) is False

    def test_a_button_press_does_not_need_an_agent(self, db):
        # A person pressing the button is saying "do this now", and their
        # laptop is awake by definition. Only the timer needs the evidence.
        thin_job(db)

        assert browse_plan.crawl_postings(db)["queued"] > 0

    def test_it_can_be_turned_off_entirely(self, db, monkeypatch):
        monkeypatch.setattr(settings, "BROWSE_ENABLED", False)
        agent_polled(db)
        thin_job(db)

        assert browse_plan.scheduled_crawl(db, PROFILE)["skipped"] == "disabled"


class TestTheTask:
    def test_it_is_registered_and_scheduled(self):
        from app.celery_app import celery_app

        assert "app.tasks.browse" in celery_app.conf.include
        entry = celery_app.conf.beat_schedule["top-up-browsing"]
        assert entry["task"] == "app.tasks.browse.top_up_browsing"

    def test_it_runs_end_to_end(self, db, monkeypatch):
        # The task opens its own session, which would be a second connection
        # and would not see this test's uncommitted transaction. Handing it
        # the test session — with `close` neutered, since the fixture owns the
        # lifecycle — is what makes the real code path observable here.
        from app.tasks import browse as module

        monkeypatch.setattr(db, "close", lambda: None)
        monkeypatch.setattr(module, "SessionLocal", lambda: db)

        agent_polled(db)
        thin_job(db)

        assert module.top_up_browsing()["queued"] > 0

    def test_a_failure_is_swallowed_rather_than_retried_forever(self, db,
                                                                monkeypatch):
        # It runs every half hour. A task that raises would fill the log with
        # the same traceback forty-eight times a day.
        from app.services import browse_plan as module
        from app.tasks.browse import top_up_browsing

        monkeypatch.setattr(
            module, "scheduled_crawl",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        outcome = top_up_browsing()

        assert outcome["queued"] == 0
        assert "boom" in outcome["error"]
