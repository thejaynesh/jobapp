"""
A crawl you asked for should not queue behind a background sweep.

The queue is never empty — enrichment fills it with LinkedIn postings, the
scheduled top-up refills it whenever it drains, and every fetch cycle adds
more. So position in that queue is the whole difference between "press the
button and watch it happen" and "press the button and find out tomorrow".

It was wrong in a way that was invisible from the panel. Everything from
`browse_plan` went in at priority 0 while enrichment's own browser work went in
at 2, so a crawl the user had just requested sat behind up to five hundred
background pages at twenty seconds each — hours. Pressing a button and seeing
nothing is indistinguishable from a broken feature, which is exactly how it was
reported.

The other half is knowing it ran at all. A crawl was queued, drained and
finished leaving nothing behind but a count, so "did my Greenhouse crawl
happen?" had no answer anywhere in the app.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.models.agent_event import AgentEvent
from app.models.browser_task import BrowserTask
from app.models.job import Job, JobStatus
from app.services import browse_plan, browser_tasks

PROFILE = {"target_roles": ["Backend Engineer"], "target_locations": ["London"]}


def thin_job(db, n=0):
    job = Job(
        source="linkedin", source_urls=[f"https://x/{uuid.uuid4()}"],
        title="Backend Engineer", company=f"Acme {n}",
        url=f"https://www.linkedin.com/jobs/view/40123456{n:02d}/",
        source_job_id=f"40123456{n:02d}", description="thin",
        status=JobStatus.new, fetched_at=datetime.now(timezone.utc),
        dedupe_hash=uuid.uuid4().hex,
    )
    db.add(job)
    db.commit()
    return job


def agent_polled(db):
    db.add(BrowserTask(
        kind="ping", payload={}, status="done",
        leased_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    ))
    db.commit()


class TestAskedForBeatsScheduled:
    def test_a_requested_crawl_outranks_the_sweep(self, db):
        agent_polled(db)
        thin_job(db, 1)
        browse_plan.scheduled_crawl(db, PROFILE)          # the timer
        browse_plan.crawl_searches(db, PROFILE, board="greenhouse")  # the button

        first = browser_tasks.lease(db, ["browse_page"], agent_id="a", limit=1)[0]
        assert "my.greenhouse.io" in first.payload["url"]

    def test_it_outranks_enrichments_browser_work_too(self, db):
        # Enrichment queues at 2. A crawl at 0 sat behind up to five hundred of
        # them, which is where the hours went.
        from app.services import enrichment

        enrichment.queue_for_browser(db, [thin_job(db, 5)])
        browse_plan.crawl_searches(db, PROFILE, board="greenhouse")

        first = browser_tasks.lease(db, None, agent_id="a", limit=1)[0]
        assert "my.greenhouse.io" in first.payload["url"]

    def test_the_scheduled_sweep_stays_at_the_back(self, db):
        agent_polled(db)
        thin_job(db, 2)
        browse_plan.scheduled_crawl(db, PROFILE)

        task = db.query(BrowserTask).filter(
            BrowserTask.kind == "browse_page").first()
        assert task.priority == browse_plan.PRIORITY_SWEEP

    def test_a_button_press_is_marked_as_requested(self, db):
        browse_plan.crawl_searches(db, PROFILE, board="greenhouse")

        task = db.query(BrowserTask).filter(
            BrowserTask.kind == "browse_page").first()
        assert task.priority == browse_plan.PRIORITY_REQUESTED

    def test_pasted_urls_are_requested_too(self, db):
        # Pasting a URL is as explicit as it gets.
        browse_plan.crawl_urls(db, "https://my.greenhouse.io/jobs/search?query=sre")

        task = db.query(BrowserTask).one()
        assert task.priority == browse_plan.PRIORITY_REQUESTED

    def test_the_scheme_is_ordered_the_way_it_reads(self):
        assert (browse_plan.PRIORITY_REQUESTED
                > browse_plan.PRIORITY_ENRICHMENT
                > browse_plan.PRIORITY_SWEEP)


class TestAskingAgainMeansAgain:
    """
    The cooloff is for the unattended pass, not for a button.

    `BROWSE_RETRY_DAYS` stops a nightly sweep re-reading the same hundred pages
    forever instead of reaching the ones behind them. Applied to a request it
    is simply wrong — pressing "crawl this board" an hour after the last crawl
    means do it again — and it turned the button into one that queued nothing,
    said nothing, and looked broken.
    """

    def test_a_board_crawled_an_hour_ago_can_be_crawled_again(self, db):
        browse_plan.crawl_searches(db, PROFILE, board="greenhouse")
        for task in db.query(BrowserTask).all():
            task.status = "done"
        db.commit()
        before = db.query(BrowserTask).count()

        browse_plan.crawl_searches(db, PROFILE, board="greenhouse")
        assert db.query(BrowserTask).count() > before

    def test_the_scheduled_sweep_still_respects_the_cooloff(self, db):
        # Otherwise the timer re-reads the same pages every half hour and never
        # reaches the backlog behind them.
        agent_polled(db)
        thin_job(db, 1)
        browse_plan.crawl_postings(db, priority=browse_plan.PRIORITY_SWEEP)
        for task in db.query(BrowserTask).all():
            task.status = "done"
        db.commit()

        assert browse_plan.crawl_postings(
            db, priority=browse_plan.PRIORITY_SWEEP)["queued"] == 0

    def test_a_page_still_in_flight_is_never_doubled(self, db):
        # The half of the rule that applies to everyone: nothing is gained by
        # opening one page twice at once.
        browse_plan.crawl_searches(db, PROFILE, board="greenhouse")
        before = db.query(BrowserTask).count()

        browse_plan.crawl_searches(db, PROFILE, board="greenhouse")
        assert db.query(BrowserTask).count() == before

    def test_the_panel_says_when_everything_is_already_queued(self, client, db,
                                                              monkeypatch):
        # Zero has three meanings and the user cannot guess which, so a silent
        # button reads as a broken one. The route reads the profile from the
        # database, so a row has to exist for there to be anything to search.
        from app.models.profile import Profile

        monkeypatch.setattr(settings, "AGENT_TOKEN", "test-token")
        db.add(Profile(data=PROFILE))
        db.commit()
        browse_plan.crawl_searches(db, PROFILE, board="greenhouse")

        body = client.post("/runs/agent/browse",
                           data={"plan": "searches", "board": "greenhouse"}).text
        assert "already" in body

    def test_the_panel_says_what_it_queued(self, client, db, monkeypatch):
        from app.models.profile import Profile

        monkeypatch.setattr(settings, "AGENT_TOKEN", "test-token")
        db.add(Profile(data=PROFILE))
        db.commit()

        body = client.post("/runs/agent/browse",
                           data={"plan": "searches", "board": "greenhouse"}).text
        assert "Queued" in body

    def test_it_says_so_when_there_is_nothing_to_search_for(self, client, db,
                                                            monkeypatch):
        # A board whose URL carries a keyword has nothing to crawl when the
        # profile names no roles — a real state, and one that used to look
        # identical to a broken button.
        monkeypatch.setattr(settings, "AGENT_TOKEN", "test-token")

        body = client.post("/runs/agent/browse",
                           data={"plan": "searches", "board": "greenhouse"}).text
        assert "Nothing to crawl" in body


class TestClearingTheQueue:
    def test_queued_pages_can_be_dropped(self, db):
        for n in range(5):
            thin_job(db, n)
        browse_plan.crawl_postings(db)

        assert browse_plan.drop_queued(db) == 5
        assert browse_plan.status(db)["waiting"] == 0

    def test_a_page_being_visited_is_left_alone(self, db):
        # Something is mid-visit; deleting the row would not close the window.
        thin_job(db, 1)
        browse_plan.crawl_postings(db)
        browser_tasks.lease(db, ["browse_page"], agent_id="a", limit=1)

        assert browse_plan.drop_queued(db) == 0

    def test_it_only_drops_browsing(self, db):
        browser_tasks.enqueue(db, "resolve_link", {"url": "https://x/1"})
        thin_job(db, 1)
        browse_plan.crawl_postings(db)

        browse_plan.drop_queued(db)
        assert db.query(BrowserTask).filter(
            BrowserTask.kind == "resolve_link").count() == 1

    def test_one_purpose_can_be_dropped_on_its_own(self, db):
        thin_job(db, 1)
        browse_plan.crawl_postings(db)
        browse_plan.crawl_searches(db, PROFILE, board="greenhouse")

        browse_plan.drop_queued(db, purpose="posting")
        left = [t.payload["purpose"] for t in db.query(BrowserTask).all()]
        assert "posting" not in left
        assert "search" in left

    def test_the_panel_offers_it_when_there_is_a_queue(self, client, db,
                                                       monkeypatch):
        monkeypatch.setattr(settings, "AGENT_TOKEN", "test-token")
        thin_job(db, 1)
        browse_plan.crawl_postings(db)

        body = client.get("/runs").text
        assert '"plan": "clear"' in body

    def test_pressing_it_clears(self, client, db, monkeypatch):
        monkeypatch.setattr(settings, "AGENT_TOKEN", "test-token")
        thin_job(db, 1)
        browse_plan.crawl_postings(db)

        client.post("/runs/agent/browse", data={"plan": "clear"})
        assert browse_plan.status(db)["waiting"] == 0


class TestSeeingThatItRan:
    def _visit(self, db, host="my.greenhouse.io", ok=True, px=48000):
        db.add(AgentEvent(
            kind="browse", host=host, ok=ok,
            summary={"purpose": "search", "title": "Jobs", "scrolled_px": px},
        ))
        db.commit()

    def test_a_visit_is_reported(self, db):
        self._visit(db)
        visits = browse_plan.recent_visits(db)

        assert visits[0]["host"] == "my.greenhouse.io"
        assert visits[0]["ok"] is True
        assert visits[0]["scrolled_px"] == 48000

    def test_how_far_it_scrolled_is_carried(self, db):
        # On an infinite-scroll board the scroll *is* the pagination, so this is
        # the only number that says whether the crawl went deep or gave up.
        from app.services import agent_work

        task = browser_tasks.enqueue(db, "browse_page", {
            "url": "https://my.greenhouse.io/jobs/search", "purpose": "search",
        })
        task.result = {"final_url": task.payload["url"], "signed_in": True,
                       "title": "Jobs", "scrolled_px": 92000}
        agent_work.ingest(db, task)

        assert browse_plan.recent_visits(db)[0]["scrolled_px"] == 92000

    def test_a_sign_in_wall_is_visible_as_one(self, db):
        self._visit(db, ok=False)
        assert browse_plan.recent_visits(db)[0]["ok"] is False

    def test_newest_first(self, db):
        self._visit(db, host="old.test")
        self._visit(db, host="new.test")

        assert browse_plan.recent_visits(db)[0]["host"] == "new.test"

    def test_nothing_opened_yet_is_an_empty_list(self, db):
        assert browse_plan.recent_visits(db) == []

    def test_the_panel_shows_them(self, client, db, monkeypatch):
        monkeypatch.setattr(settings, "AGENT_TOKEN", "test-token")
        self._visit(db)

        body = client.get("/runs").text
        assert "Pages opened" in body
        assert "my.greenhouse.io" in body

    def test_the_panel_shows_a_signed_out_visit_differently(self, client, db,
                                                             monkeypatch):
        # The one failure a harvest cannot describe: the page rendered a login
        # instead of the listing, so nothing was found and it looks identical to
        # a reader whose fields moved.
        monkeypatch.setattr(settings, "AGENT_TOKEN", "test-token")
        self._visit(db, ok=False)

        assert "signed out" in client.get("/runs").text
