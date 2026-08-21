"""
Pausing one site without pausing the rest.

Written because LinkedIn sent a real warning: "we noticed some unusual activity
on your account... your account has accessed a high volume of LinkedIn profile
data... this often happens because of a third-party tool or browser extension."

That is an accurate description of driven browsing. It opens up to
`BROWSE_MAX_QUEUED` pages a run, on a schedule, through a logged-in session,
and the description backlog behind it is tens of thousands of postings on one
host. The cost of ignoring it is the account, not the run.

Two things had to be true and neither was:

1. **Stopping one site could not cost the others.** The only switch that
   actually stopped LinkedIn traffic was the global "open pages in a hidden
   window" toggle, which also stopped the Greenhouse crawl that had just
   started working. Pausing is per host.

2. **A pause has to cover every path to the queue.** `browse_plan.enqueue` was
   not the only door: `enrichment.plan_browser_queue` calls
   `browser_tasks.enqueue` directly, and it is the *louder* of the two. A
   pause honoured in one and not the other is not a pause.

Harvesting is deliberately untouched. Reading job data out of pages the user
opens themselves makes no extra requests and is not what gets an account
flagged — opening sixty pages a run is. Conflating the two would have thrown
away the safe half of the feature to fix the risky half.
"""

import uuid
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.models.browser_task import BrowserTask
from app.models.job import Job, JobStatus
from app.services import browse_plan, browser_tasks

PROFILE = {"target_roles": ["Backend Engineer"], "target_locations": ["London"]}


def thin_job(db, url, source="linkedin"):
    job = Job(
        source=source, source_urls=[url],
        title="Backend Engineer", company=f"Acme {uuid.uuid4().hex[:6]}",
        url=url, description="thin",
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


def queued_urls(db):
    return [
        row.payload["url"]
        for row in db.query(BrowserTask).filter(
            BrowserTask.status == "queued",
            BrowserTask.kind.in_(("browse_page", "resolve_link")),
        )
    ]


class TestReadingThePauseList:
    def test_an_empty_setting_pauses_nothing(self, monkeypatch):
        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "")
        assert browse_plan.paused_hosts() == ()
        assert not browse_plan.is_paused("https://www.linkedin.com/jobs/view/1/")

    def test_a_host_covers_its_subdomains(self, monkeypatch):
        # `linkedin.com` has to be enough. Requiring `www.linkedin.com` would
        # mean a pause that misses `uk.linkedin.com`, and finding that out is
        # the expensive way.
        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "linkedin.com")
        assert browse_plan.is_paused("https://www.linkedin.com/jobs/view/1/")
        assert browse_plan.is_paused("https://uk.linkedin.com/jobs/view/1/")
        assert browse_plan.is_paused("https://linkedin.com/jobs/view/1/")

    def test_it_does_not_pause_a_host_that_merely_ends_similarly(self, monkeypatch):
        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "linkedin.com")
        assert not browse_plan.is_paused("https://notlinkedin.com/jobs/1")
        assert not browse_plan.is_paused("https://my.greenhouse.io/jobs/search")

    def test_several_hosts_and_untidy_input(self, monkeypatch):
        # Typed into a .env by hand, so spaces, a stray leading dot and a
        # trailing comma all have to work rather than silently pause nothing.
        monkeypatch.setattr(
            settings, "BROWSE_PAUSED_HOSTS", " linkedin.com , .indeed.com ,",
        )
        assert browse_plan.paused_hosts() == ("linkedin.com", "indeed.com")
        assert browse_plan.is_paused("https://www.indeed.com/viewjob?jk=1")

    def test_a_malformed_url_is_not_paused_and_not_an_error(self, monkeypatch):
        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "linkedin.com")
        assert not browse_plan.is_paused("")
        assert not browse_plan.is_paused(None)
        assert not browse_plan.is_paused("not a url")


class TestNothingIsQueuedForAPausedHost:
    def test_a_crawl_skips_the_paused_board_and_keeps_the_others(self, db, monkeypatch):
        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "linkedin.com")
        browse_plan.crawl_searches(db, PROFILE)

        urls = queued_urls(db)
        assert urls, "the other boards still have to be crawled"
        assert not any("linkedin.com" in url for url in urls)

    def test_asking_for_the_paused_board_by_name_queues_nothing(self, db, monkeypatch):
        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "linkedin.com")
        outcome = browse_plan.crawl_searches(db, PROFILE, board="linkedin")
        assert outcome["queued"] == 0

    def test_a_pasted_url_is_refused_too(self, db, monkeypatch):
        """
        The paste box is the one path a user drives by hand, and it is exactly
        where a pause would be most tempting to skip. It is also where the
        mistake is easiest to make — pasting a LinkedIn search you were just
        looking at, days after pausing it.
        """
        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "linkedin.com")
        outcome = browse_plan.crawl_urls(
            db, "https://www.linkedin.com/jobs/search/?keywords=go",
        )
        assert outcome["queued"] == 0
        assert queued_urls(db) == []

    def test_postings_on_a_paused_host_are_not_revisited(self, db, monkeypatch):
        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "linkedin.com")
        thin_job(db, "https://www.linkedin.com/jobs/view/4012345601/")
        outcome = browse_plan.crawl_postings(db)
        assert outcome["queued"] == 0

    def test_the_scheduled_sweep_honours_it(self, db, monkeypatch):
        # The sweep is the one nobody watches, so a pause it ignored would undo
        # itself quietly every thirty minutes.
        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "linkedin.com")
        agent_polled(db)
        thin_job(db, "https://www.linkedin.com/jobs/view/4012345602/")
        browse_plan.scheduled_crawl(db, PROFILE)
        assert not any("linkedin.com" in url for url in queued_urls(db))


class TestEnrichmentHonoursItToo:
    """
    The louder path, and the one that bypasses `browse_plan` entirely. A crawl
    queues sixty pages; this queues from a backlog of tens of thousands on one
    host, which is the volume that gets noticed.
    """

    def test_a_paused_posting_is_not_queued(self, db, monkeypatch):
        from app.services import enrichment

        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "linkedin.com")
        job = thin_job(db, "https://www.linkedin.com/jobs/view/4012345603/")
        assert enrichment.queue_for_browser(db, [job]) == 0
        assert queued_urls(db) == []

    def test_an_unpaused_posting_still_is(self, db, monkeypatch):
        from app.services import enrichment

        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "linkedin.com")
        job = thin_job(
            db, "https://www.dice.com/job-detail/abc123", source="dice_harvest",
        )
        assert enrichment.queue_for_browser(db, [job]) == 1

    def test_a_paused_job_is_reported_separately_from_a_full_queue(self, db, monkeypatch):
        """
        Both produce a zero and only one of them is temporary. Congestion clears
        in an hour; a pause is a decision, and the caller has to treat them
        differently or it will put paused jobs to sleep for the wrong reason.
        """
        from app.services import enrichment

        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "linkedin.com")
        job = thin_job(db, "https://www.linkedin.com/jobs/view/4012345604/")
        queued, paused = enrichment.plan_browser_queue(db, [job])
        assert queued == []
        assert [j.id for j in paused] == [job.id]

    def test_a_paused_host_is_still_reported_when_the_queue_is_full(self, db, monkeypatch):
        from app.services import enrichment

        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "linkedin.com")
        monkeypatch.setattr(settings, "ENRICH_MAX_BROWSER_OUTSTANDING", 0)
        job = thin_job(db, "https://www.linkedin.com/jobs/view/4012345605/")
        queued, paused = enrichment.plan_browser_queue(db, [job])
        assert queued == []
        assert [j.id for j in paused] == [job.id]

    def test_a_paused_job_does_not_jam_the_queue_forever(self, db, monkeypatch):
        """
        The failure this prevents, and it has bitten once already in this
        codebase: a browser-tier job that is never stamped stays at the head of
        a newest-first ordering, so every pass picks it again and the hosts that
        *are* running never get reached. A paused job can never be improved, so
        it has to be stamped or it starves everything behind it.
        """
        from app.services import enrichment

        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "linkedin.com")
        job = thin_job(db, "https://www.linkedin.com/jobs/view/4012345606/")
        enrichment.enrich_jobs(db, [job])
        db.refresh(job)
        assert job.enrichment_attempted_at is not None

    def test_the_stamp_lands_on_the_jobs_actually_queued(self, db, monkeypatch):
        """
        It used to stamp `for_browser[:count]`, which assumed the queued jobs
        were a prefix of the list. A skip in the middle — a paused host, a URL
        already waiting — stamped a job that was never queued and left a queued
        one to be picked again next pass.
        """
        from app.services import enrichment

        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "linkedin.com")
        paused_job = thin_job(db, "https://www.linkedin.com/jobs/view/4012345607/")
        live_job = thin_job(
            db, "https://www.dice.com/job-detail/def456", source="dice_harvest",
        )
        queued, paused = enrichment.plan_browser_queue(db, [paused_job, live_job])
        assert [j.id for j in queued] == [live_job.id]
        assert [j.id for j in paused] == [paused_job.id]


class TestClearingWhatWasAlreadyQueued:
    """
    Pausing stops new work, but the queue is a plan made earlier — and after a
    site has warned you, sixty of its pages already waiting is the problem
    itself rather than a detail.
    """

    def test_queued_pages_for_a_paused_host_are_dropped(self, db, monkeypatch):
        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "")
        browse_plan.crawl_searches(db, PROFILE, board="linkedin")
        assert any("linkedin.com" in url for url in queued_urls(db))

        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "linkedin.com")
        assert browse_plan.drop_paused(db) > 0
        assert not any("linkedin.com" in url for url in queued_urls(db))

    def test_other_hosts_pages_are_left_alone(self, db, monkeypatch):
        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "")
        browse_plan.crawl_searches(db, PROFILE)
        before = [u for u in queued_urls(db) if "linkedin.com" not in u]
        assert before

        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "linkedin.com")
        browse_plan.drop_paused(db)
        assert sorted(u for u in queued_urls(db) if "linkedin.com" not in u) == \
            sorted(before)

    def test_a_page_mid_visit_is_left_alone(self, db, monkeypatch):
        # Deleting a leased row would not close the window, so it buys nothing
        # and loses the result of a visit already paid for.
        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "")
        browse_plan.crawl_searches(db, PROFILE, board="linkedin")
        leased = browser_tasks.lease(db, ["browse_page"], agent_id="a", limit=1)
        assert leased

        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "linkedin.com")
        browse_plan.drop_paused(db)
        still_there = db.query(BrowserTask).filter(
            BrowserTask.id == leased[0].id).one_or_none()
        assert still_there is not None

    def test_dropping_nothing_is_not_an_error(self, db, monkeypatch):
        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "linkedin.com")
        assert browse_plan.drop_paused(db) == 0

    def test_the_sweep_does_not_count_paused_pages_as_a_draining_queue(
        self, db, monkeypatch,
    ):
        """
        The top-up only runs when the queue is nearly empty. Pages for a paused
        host will never run, so counting them holds the sweep off on the
        strength of a backlog that is going nowhere — the pause would quietly
        stop the *other* boards being crawled too.
        """
        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "")
        monkeypatch.setattr(settings, "BROWSE_TOPUP_BELOW", 2)
        agent_polled(db)
        browse_plan.crawl_searches(db, PROFILE, board="linkedin")
        assert len(queued_urls(db)) > 2

        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "linkedin.com")
        outcome = browse_plan.scheduled_crawl(db, PROFILE)
        assert outcome["skipped"] != "queue still draining"


class TestWhatThePanelSays:
    def test_the_paused_hosts_are_named(self, db, monkeypatch):
        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "linkedin.com")
        assert browse_plan.status(db)["paused"] == ["linkedin.com"]

    def test_a_paused_board_is_marked_rather_than_hidden(self, db, monkeypatch):
        # A board that vanished from the panel reads as a bug; one labelled
        # paused reads as a decision, which is what it was.
        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "linkedin.com")
        boards = {b["key"]: b for b in browse_plan.status(db)["boards"]}
        assert boards["linkedin"]["paused"] is True
        assert boards["greenhouse"]["paused"] is False

    def test_nothing_is_claimed_when_nothing_is_paused(self, db, monkeypatch):
        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "")
        status = browse_plan.status(db)
        assert status["paused"] == []
        assert not any(b["paused"] for b in status["boards"])

    def test_the_page_renders_with_a_board_paused(self, db, monkeypatch, client):
        monkeypatch.setattr(settings, "AGENT_TOKEN", "t")
        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "linkedin.com")
        page = client.get("/runs")
        assert page.status_code == 200
        assert "linkedin.com" in page.text

    def test_a_zero_says_paused_rather_than_already_waiting(
        self, db, monkeypatch, client,
    ):
        """
        The old message claimed the pages were "already waiting in the queue",
        which for a paused host is a flat lie about work that will never run —
        and indistinguishable from the broken-button reading the user already
        arrived at once.
        """
        monkeypatch.setattr(settings, "AGENT_TOKEN", "t")
        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "linkedin.com")
        response = client.post(
            "/runs/agent/browse", data={"plan": "searches", "board": "linkedin"},
        )
        assert response.status_code == 200
        assert "paused" in response.text.lower()


class TestHarvestingIsNotPaused:
    """
    The distinction the whole design rests on. Harvesting reads job data out of
    pages the user opens anyway and makes no extra requests; browsing opens
    pages on its own. Only the second is what a site notices, and collapsing
    them would throw away the safe half to fix the risky half.
    """

    def test_a_paused_host_still_stores_what_it_sends(self, db, monkeypatch):
        from app.services.harvest import save_harvested_jobs

        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "linkedin.com")
        counts = save_harvested_jobs(db, [{
            "source": "linkedin_harvest",
            "source_job_id": "4099999999",
            "url": "https://www.linkedin.com/jobs/view/4099999999/",
            "title": "Backend Engineer", "company": "Acme",
            "location": "London", "description": "A real description.",
        }])
        assert counts["inserted"] == 1

    def test_pausing_leaves_the_site_readable(self, monkeypatch):
        # `HARVEST_SOURCES` is what decides whether a payload is read at all,
        # and pausing must not touch it: a page the user opens themselves is
        # still worth reading.
        from app.services.harvest import source_for_url

        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "linkedin.com")
        assert source_for_url(
            "https://www.linkedin.com/jobs/view/1/") == "linkedin_harvest"
