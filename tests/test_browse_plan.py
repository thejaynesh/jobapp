"""
Having the extension open pages so nobody has to.

The harvest was never limited by what it could read — it reads LinkedIn's own
API responses and gets more than the guest API returns. It was limited by
attendance: nothing is harvested from a page nobody opened, so covering a
search meant clicking through it by hand.

So this queues the visiting, and almost nothing else. There is no parser here
and no extraction: a `browse_page` task opens a URL, the interceptor reads the
page's own traffic exactly as it would if you had opened it, and the jobs
arrive through the harvest endpoint that already existed.

The tests that matter are the ones about restraint. This drives a real browser
through a logged-in session, so the run is capped, the pages are spaced, and a
page opened recently is not opened again — and the cost of those being wrong is
the user's account rather than a failed run.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.models.browser_task import BrowserTask
from app.models.job import Job, JobStatus
from app.services import browse_plan

PROFILE = {
    "target_roles": ["Backend Engineer", "Platform Engineer"],
    "target_locations": ["London", "Remote"],
}


def make_job(db, **overrides):
    fields = {
        "source": "linkedin",
        "source_urls": [f"https://x/{uuid.uuid4()}"],
        "title": "Backend Engineer",
        "company": "Acme",
        "url": "https://www.linkedin.com/jobs/view/4012345678/",
        "source_job_id": str(uuid.uuid4().int)[:10],
        "description": "A teaser.",
        "status": JobStatus.new,
        "fetched_at": datetime.now(timezone.utc),
        "dedupe_hash": uuid.uuid4().hex,
    }
    fields.update(overrides)
    job = Job(**fields)
    db.add(job)
    db.commit()
    return job


def queued(db):
    return [
        task.payload["url"]
        for task in db.query(BrowserTask).filter(BrowserTask.kind == "browse_page").all()
    ]


class TestWhichSearchesItWalks:
    def test_one_per_role_and_location(self, db):
        urls = browse_plan.search_urls(PROFILE)
        assert len(urls) == 4
        assert all(url.startswith("https://www.linkedin.com/jobs/search/") for url in urls)

    def test_the_role_and_location_are_encoded(self, db):
        urls = browse_plan.search_urls({"target_roles": ["Site Reliability Engineer"],
                                        "target_locations": ["New York, NY"]})
        assert "keywords=Site+Reliability+Engineer" in urls[0]
        assert "location=New+York%2C+NY" in urls[0]

    def test_it_asks_for_the_last_week_only(self, db):
        # Without this the budget goes on re-reading postings from months ago
        # that are already stored, which is the expensive way to harvest
        # nothing.
        assert "f_TPR=r604800" in browse_plan.search_urls(PROFILE)[0]

    def test_no_target_roles_means_no_searches(self, db):
        assert browse_plan.search_urls({"target_locations": ["London"]}) == []
        assert browse_plan.search_urls(None) == []

    def test_a_profile_with_no_locations_still_searches(self, db):
        urls = browse_plan.search_urls({"target_roles": ["Backend Engineer"]})
        assert len(urls) == 1


class TestWhichPostingsItOpens:
    def test_a_linkedin_job_with_a_thin_description(self, db):
        # The case that pays: a harvested search card has a title and an id and
        # no body, and the guest API cannot fix that.
        make_job(db, source_job_id="4012345678", description="Short teaser.")
        assert browse_plan.posting_urls(db) == [
            "https://www.linkedin.com/jobs/view/4012345678/"
        ]

    def test_a_job_that_already_has_its_description_is_left_alone(self, db):
        make_job(db, description="x" * 5000)
        assert browse_plan.posting_urls(db) == []

    def test_a_closed_posting_is_not_reopened(self, db):
        make_job(db, description="thin",
                 closed_at=datetime.now(timezone.utc), closed_note="404")
        assert browse_plan.posting_urls(db) == []

    def test_jobs_from_other_sources_are_not_opened(self, db):
        # Opening greenhouse.io in a LinkedIn crawl would be both useless and
        # a URL built out of somebody else's id.
        make_job(db, source="greenhouse", url="https://boards.greenhouse.io/a/jobs/1",
                 source_job_id="55", description="thin")
        assert browse_plan.posting_urls(db) == []

    def test_harvested_jobs_count_too(self, db):
        make_job(db, source="linkedin_harvest", source_job_id="777888999",
                 description="thin")
        assert browse_plan.posting_urls(db) == [
            "https://www.linkedin.com/jobs/view/777888999/"
        ]

    def test_the_url_is_rebuilt_from_the_id(self, db):
        # A harvested link carries tracking parameters, and two of them for the
        # same posting would be two tasks opening one page.
        make_job(
            db, source_job_id="4012345678", description="thin",
            url="https://www.linkedin.com/jobs/view/4012345678/?refId=abc&trk=xyz",
        )
        assert browse_plan.posting_urls(db) == [
            "https://www.linkedin.com/jobs/view/4012345678/"
        ]

    def test_a_job_with_no_id_falls_back_to_its_url_without_the_query(self, db):
        make_job(db, source_job_id=None, description="thin",
                 url="https://www.linkedin.com/jobs/view/slug-name?trk=xyz")
        assert browse_plan.posting_urls(db) == [
            "https://www.linkedin.com/jobs/view/slug-name"
        ]

    def test_two_jobs_sharing_a_posting_are_one_page(self, db):
        make_job(db, source_job_id="4012345678", description="thin")
        make_job(db, source_job_id="4012345678", description="thin",
                 company="Acme Europe")
        assert len(browse_plan.posting_urls(db)) == 1


class TestRestraint:
    """
    This drives a real browser through a logged-in session. Volume and rhythm
    are what anti-automation systems measure, and the cost of getting these
    wrong is the account rather than the run.
    """

    def test_a_run_is_capped(self, db, monkeypatch):
        monkeypatch.setattr(settings, "BROWSE_MAX_QUEUED", 5)
        for _ in range(12):
            make_job(db, description="thin")

        browse_plan.crawl_postings(db)
        assert len(queued(db)) == 5

    def test_the_cap_cannot_be_argued_past(self, db, monkeypatch):
        # The limit is a ceiling, not a default: a caller asking for 500 gets
        # the configured maximum.
        monkeypatch.setattr(settings, "BROWSE_MAX_QUEUED", 3)
        for _ in range(20):
            make_job(db, description="thin")

        browse_plan.crawl_postings(db, limit=500)
        assert len(queued(db)) == 3

    def test_every_task_carries_the_pace(self, db, monkeypatch):
        # Sent per task rather than read from the client's own settings, so the
        # rhythm is one decision made in one place.
        monkeypatch.setattr(settings, "BROWSE_GAP_SECONDS", 30)
        monkeypatch.setattr(settings, "BROWSE_SETTLE_SECONDS", 8)
        make_job(db, description="thin")

        browse_plan.crawl_postings(db)
        payload = db.query(BrowserTask).one().payload
        assert payload["gap_seconds"] == 30
        assert payload["settle_seconds"] == 8

    def test_a_page_already_queued_is_not_queued_twice(self, db):
        make_job(db, source_job_id="4012345678", description="thin")
        browse_plan.crawl_postings(db)
        browse_plan.crawl_postings(db)

        assert len(queued(db)) == 1

    def test_a_page_browsed_recently_is_not_reopened(self, db):
        # Otherwise a nightly run re-reads the same hundred postings forever
        # and never reaches the ones behind them.
        make_job(db, source_job_id="4012345678", description="thin")
        browse_plan.crawl_postings(db)
        task = db.query(BrowserTask).one()
        task.status = "done"
        db.commit()

        browse_plan.crawl_postings(db)
        assert len(queued(db)) == 1

    def test_it_tries_again_after_the_cooloff(self, db, monkeypatch):
        monkeypatch.setattr(settings, "BROWSE_RETRY_DAYS", 30)
        make_job(db, source_job_id="4012345678", description="thin")
        browse_plan.crawl_postings(db)
        task = db.query(BrowserTask).one()
        task.status = "done"
        task.created_at = datetime.now(timezone.utc) - timedelta(days=60)
        db.commit()

        browse_plan.crawl_postings(db)
        assert len(queued(db)) == 2

    def test_it_can_be_turned_off(self, db, monkeypatch):
        monkeypatch.setattr(settings, "BROWSE_ENABLED", False)
        make_job(db, description="thin")

        assert browse_plan.crawl_postings(db)["queued"] == 0
        assert queued(db) == []

    def test_browsing_never_jumps_the_queue(self, db):
        # Link resolution and enrichment fetches answer a question something is
        # waiting on. This is a background sweep.
        make_job(db, description="thin")
        browse_plan.crawl_postings(db)

        assert db.query(BrowserTask).one().priority == 0


class TestStatus:
    def test_it_says_how_long_the_queue_will_take(self, db, monkeypatch):
        # "60 pages queued" means nothing without it, and the number being
        # large is the feature rather than a problem to fix.
        monkeypatch.setattr(settings, "BROWSE_GAP_SECONDS", 20)
        monkeypatch.setattr(settings, "BROWSE_SETTLE_SECONDS", 10)
        for _ in range(10):
            make_job(db, description="thin")
        browse_plan.crawl_postings(db)

        status = browse_plan.status(db)
        assert status["waiting"] == 10
        assert status["eta_minutes"] == 5

    def test_an_empty_queue_has_no_eta(self, db):
        assert browse_plan.status(db)["eta_minutes"] == 0


class TestTheTaskAndItsResult:
    def test_browse_page_is_a_known_kind(self):
        from app.models.browser_task import TASK_KINDS

        assert "browse_page" in TASK_KINDS

    def test_a_visit_is_recorded(self, db):
        from app.models.agent_event import AgentEvent
        from app.services import agent_work, browser_tasks

        task = browser_tasks.enqueue(db, "browse_page", {
            "url": "https://www.linkedin.com/jobs/view/4012345678/",
            "purpose": "posting",
        })
        task.result = {"final_url": task.payload["url"], "signed_in": True,
                       "title": "Backend Engineer | Acme"}
        agent_work.ingest(db, task)

        event = db.query(AgentEvent).filter(AgentEvent.kind == "browse").one()
        assert event.ok is True
        assert event.host == "www.linkedin.com"

    def test_a_sign_in_wall_is_reported_as_a_failure(self, db):
        # The one failure a harvest cannot report on its own: a login wall
        # renders instead of the posting, so nothing is found — which looks
        # exactly like a reader whose field names moved.
        from app.models.agent_event import AgentEvent
        from app.services import agent_work, browser_tasks

        task = browser_tasks.enqueue(db, "browse_page", {
            "url": "https://www.linkedin.com/jobs/view/4012345678/",
        })
        task.result = {"final_url": "https://www.linkedin.com/login",
                       "signed_in": False, "title": "Sign In | LinkedIn"}
        agent_work.ingest(db, task)

        event = db.query(AgentEvent).filter(AgentEvent.kind == "browse").one()
        assert event.ok is False

    def test_ingesting_a_result_stores_nothing_about_jobs(self, db):
        # Deliberate: the interceptor already posted whatever the page showed
        # to /harvest while it was open. Parsing here would be a second, worse
        # reader of the same data.
        from app.services import agent_work, browser_tasks

        task = browser_tasks.enqueue(db, "browse_page", {"url": "https://x/1"})
        task.result = {"final_url": "https://x/1", "signed_in": True}
        agent_work.ingest(db, task)

        assert db.query(Job).count() == 0


class TestTheButtons:
    @pytest.fixture(autouse=True)
    def _agent_configured(self, monkeypatch):
        # The whole agent block is hidden until the API has a token, which is
        # right — offering to queue work for an agent that cannot authenticate
        # would be a button that silently does nothing.
        monkeypatch.setattr(settings, "AGENT_TOKEN", "test-token")

    def test_the_panel_offers_both_plans(self, client, db):
        body = client.get("/runs").text
        assert "/runs/agent/browse" in body
        assert "Fill in descriptions" in body
        assert "Crawl searches" in body

    def test_the_panel_says_it_is_slow_on_purpose(self, client, db):
        # A user who does not know the pacing is deliberate will read a slow
        # queue as a bug and go looking for the throttle.
        assert "slow on purpose" in client.get("/runs").text

    def test_pressing_fill_in_descriptions_queues_postings(self, client, db):
        make_job(db, source_job_id="4012345678", description="thin")
        client.post("/runs/agent/browse", data={"plan": "postings"})

        assert queued(db) == ["https://www.linkedin.com/jobs/view/4012345678/"]

    def test_pressing_crawl_searches_queues_searches(self, client, db):
        from app.models.profile import Profile

        db.add(Profile(data=PROFILE))
        db.commit()
        client.post("/runs/agent/browse", data={"plan": "searches"})

        assert len(queued(db)) == 4

    def test_a_failure_does_not_take_the_page_down(self, client, db, monkeypatch):
        from app.services import browse_plan as module

        monkeypatch.setattr(module, "crawl_postings",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        assert client.post("/runs/agent/browse", data={"plan": "postings"}).status_code == 200
