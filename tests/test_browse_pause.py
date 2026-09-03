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


class TestASiteAskingForAHumanCheck:
    """
    Jooble puts a Cloudflare check in front of its `away` redirects — which is
    exactly the URL link resolution has to open to find the employer's real
    apply link.

    The window was opened minimized and closed a few seconds later, so the
    check appeared and vanished with nobody able to click it. Every visit
    failed, and from the panel it looked identical to a page that simply had
    nothing on it — which points at the reader, and the reader was fine.

    Two halves. The extension raises the window and waits for the user, which
    is the part that actually fixes it. This is the other half: once a host has
    asked and nobody answered, stop queueing for it. Sixty pending Jooble
    visits would otherwise be sixty raised windows at an empty chair, with
    every working board stuck behind them.
    """

    def blocked_event(self, db, host, outcome="timeout", hours_ago=0):
        from app.models.agent_event import AgentEvent

        row = AgentEvent(
            kind="browse", host=host, ok=False,
            summary={"purpose": "enrich", "challenge": outcome},
        )
        db.add(row)
        db.flush()
        if hours_ago:
            row.created_at = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        db.commit()
        return row

    def test_a_host_that_asked_and_got_no_answer_is_backed_off(self, db):
        self.blocked_event(db, "jooble.org")
        assert "jooble.org" in browse_plan.blocked_hosts(db)

    def test_passing_the_check_is_not_held_against_the_host(self, db):
        # Getting through is the outcome we want. Counting it as a failure
        # would punish the click and stop the pages it just unlocked.
        self.blocked_event(db, "jooble.org", outcome="passed")
        assert browse_plan.blocked_hosts(db) == set()

    def test_an_ordinary_visit_is_not_a_block(self, db):
        from app.models.agent_event import AgentEvent

        db.add(AgentEvent(kind="browse", host="jooble.org", ok=True,
                          summary={"purpose": "harvest", "challenge": ""}))
        db.commit()
        assert browse_plan.blocked_hosts(db) == set()

    def test_the_backoff_expires(self, db, monkeypatch):
        # These checks are usually about the traffic pattern rather than the
        # visitor, so a host that blocked us this morning is worth one attempt
        # tomorrow. A permanent verdict from one bad evening would be wrong.
        monkeypatch.setattr(settings, "BROWSE_CHALLENGE_BACKOFF_HOURS", 24)
        self.blocked_event(db, "jooble.org", hours_ago=30)
        assert browse_plan.blocked_hosts(db) == set()

    def test_nothing_is_queued_for_a_blocked_host(self, db, monkeypatch):
        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "")
        self.blocked_event(db, "jooble.org")
        queued = browse_plan.enqueue(
            db, ["https://jooble.org/away/12345"],
            priority=browse_plan.PRIORITY_SWEEP,
        )
        assert queued == 0

    def test_a_crawl_you_asked_for_tries_anyway(self, db, monkeypatch):
        """
        The backoff's whole premise is that nobody is at the keyboard. Pressing
        a button disproves that, so a request the user is watching ignores it —
        the same reasoning that lets a requested crawl skip the re-visit
        cooloff.
        """
        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "")
        self.blocked_event(db, "jooble.org")
        queued = browse_plan.enqueue(
            db, ["https://jooble.org/away/12345"],
            priority=browse_plan.PRIORITY_REQUESTED,
        )
        assert queued == 1

    def test_enrichment_stops_queueing_for_it_too(self, db, monkeypatch):
        from app.services import enrichment

        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "")
        self.blocked_event(db, "jooble.org")
        job = thin_job(db, "https://jooble.org/away/999", source="jooble")
        queued, deferred = enrichment.plan_browser_queue(db, [job])
        assert queued == []
        assert [j.id for j in deferred] == [job.id]

    def test_a_blocked_job_does_not_jam_the_queue(self, db, monkeypatch):
        # Same failure as a paused one: browser-only, unreachable, and left
        # unstamped it sits at the head of a newest-first ordering forever.
        from app.services import enrichment

        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "")
        self.blocked_event(db, "jooble.org")
        job = thin_job(db, "https://jooble.org/away/998", source="jooble")
        enrichment.enrich_jobs(db, [job])
        db.refresh(job)
        assert job.enrichment_attempted_at is not None

    def test_a_subdomain_is_covered(self, db):
        self.blocked_event(db, "jooble.org")
        blocked = browse_plan.blocked_hosts(db)
        assert browse_plan.is_blocked("uk.jooble.org", blocked)
        assert not browse_plan.is_blocked("notjooble.org", blocked)

    def test_the_panel_names_it(self, db):
        self.blocked_event(db, "jooble.org")
        assert browse_plan.status(db)["blocked"] == ["jooble.org"]

    def test_the_panel_says_nothing_when_no_host_is_blocking(self, db):
        assert browse_plan.status(db)["blocked"] == []


class TestRecordingWhatTheBrowserSaw:
    """
    The extension reports how a check went. Getting this wrong in the obvious
    direction — counting a blocked visit as a successful one — is what made the
    harvest-health panel accuse a working reader of having broken.
    """

    def finished(self, db, challenge, url="https://jooble.org/away/1"):
        from app.models.browser_task import BrowserTask
        from app.services.agent_work import _ingest_browse_page

        task = BrowserTask(
            kind="browse_page", status="done", agent_id="a",
            payload={"url": url, "purpose": "enrich"},
            result={"final_url": url, "signed_in": True, "challenge": challenge},
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        db.add(task)
        db.commit()
        _ingest_browse_page(db, task)
        return task

    def latest(self, db):
        from app.models.agent_event import AgentEvent

        return (
            db.query(AgentEvent)
            .filter(AgentEvent.kind == "browse")
            .order_by(AgentEvent.created_at.desc())
            .first()
        )

    def test_an_unanswered_check_is_not_a_successful_visit(self, db):
        self.finished(db, "timeout")
        assert self.latest(db).ok is False

    def test_passing_the_check_is(self, db):
        # The page was reached in the end, which is the thing `ok` means.
        self.finished(db, "passed")
        assert self.latest(db).ok is True

    def test_a_page_that_never_asked_is_unaffected(self, db):
        self.finished(db, "")
        assert self.latest(db).ok is True

    def test_the_outcome_is_kept_not_just_the_verdict(self, db):
        # "nobody was there" and "the extension declined to ask again" lead to
        # the same backoff but are different things to read on a panel.
        self.finished(db, "skipped")
        assert self.latest(db).summary["challenge"] == "skipped"

    def test_an_old_extension_reporting_nothing_still_works(self, db):
        from app.models.browser_task import BrowserTask
        from app.services.agent_work import _ingest_browse_page

        task = BrowserTask(
            kind="browse_page", status="done", agent_id="a",
            payload={"url": "https://jooble.org/away/2"},
            result={"final_url": "https://jooble.org/away/2", "signed_in": True},
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        db.add(task)
        db.commit()
        _ingest_browse_page(db, task)
        assert self.latest(db).ok is True


class TestABoardThatAsksUsToSlowDown:
    """
    Greenhouse's board rate-limits an infinite scroll: after enough passes it
    stops sending cards and asks you to wait a few minutes.

    The scroll loop did not notice. It carried on scrolling into a closed door,
    burning the rest of its 75-second budget on requests that could not
    succeed — which is how "a few minutes" becomes longer, and how the visit
    reported a shallow crawl that looked like a broken scroll target.

    Three things follow from a limit being a *rate*:

    * Stop the moment it appears. Further passes buy nothing and cost goodwill.
    * Rest the host for minutes, not hours. It said "not this fast", not "not
      at all" — and the pages harvested before the limit are already stored, so
      there is nothing to recover, only a question of when to go back.
    * Go slower next time rather than merely shorter. A pause between batches
      reaches further than a smaller pass count does, because the pass count
      only decides where you stop.
    """

    def limited_at(self, db, host, passes, minutes_ago=0):
        from app.models.agent_event import AgentEvent

        row = AgentEvent(
            kind="browse", host=host, ok=True,
            summary={"purpose": "harvest", "rate_limited": True,
                     "passes_done": passes},
        )
        db.add(row)
        db.flush()
        if minutes_ago:
            row.created_at = datetime.now(timezone.utc) - timedelta(
                minutes=minutes_ago)
        db.commit()
        return row

    def test_a_host_that_asked_us_to_wait_is_rested(self, db):
        self.limited_at(db, "my.greenhouse.io", 40)
        assert "my.greenhouse.io" in browse_plan.resting_hosts(db)

    def test_the_rest_is_minutes_not_hours(self, db, monkeypatch):
        # The distinction from a human check. That is a door somebody has to
        # open; this is a pace, and it clears on its own.
        monkeypatch.setattr(settings, "BROWSE_RATELIMIT_REST_MINUTES", 20)
        self.limited_at(db, "my.greenhouse.io", 40, minutes_ago=25)
        assert browse_plan.resting_hosts(db) == set()

    def test_an_ordinary_visit_is_not_a_rest(self, db):
        from app.models.agent_event import AgentEvent

        db.add(AgentEvent(kind="browse", host="my.greenhouse.io", ok=True,
                          summary={"purpose": "harvest", "rate_limited": False,
                                   "passes_done": 25}))
        db.commit()
        assert browse_plan.resting_hosts(db) == set()

    def test_nothing_is_queued_while_a_host_is_resting(self, db, monkeypatch):
        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "")
        self.limited_at(db, "my.greenhouse.io", 40)
        queued = browse_plan.enqueue(
            db, ["https://my.greenhouse.io/jobs/search?query=x"],
            priority=browse_plan.PRIORITY_SWEEP,
        )
        assert queued == 0

    def test_a_button_press_does_not_override_a_rest(self, db, monkeypatch):
        """
        The one backoff a request does not beat, and the reason is the
        difference between the two kinds. A human check being watched by a
        human is answerable — that is exactly what it wants. A board that said
        "wait a few minutes" says it just as firmly to somebody sitting at the
        keyboard, so queueing anyway spends a request to be told again.
        """
        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "")
        self.limited_at(db, "my.greenhouse.io", 40)
        queued = browse_plan.enqueue(
            db, ["https://my.greenhouse.io/jobs/search?query=x"],
            priority=browse_plan.PRIORITY_REQUESTED,
        )
        assert queued == 0

    def test_the_button_says_when_rather_than_no(self, db, monkeypatch, client):
        # A silent zero from a button is the failure this codebase has already
        # produced twice. "Wait twenty minutes" is an answer; nothing is not.
        monkeypatch.setattr(settings, "AGENT_TOKEN", "t")
        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "")
        self.limited_at(db, "my.greenhouse.io", 40)
        response = client.post(
            "/runs/agent/browse",
            data={"plan": "searches", "board": "greenhouse"},
        )
        assert response.status_code == 200
        assert "slow down" in response.text.lower()

    def test_other_boards_keep_running(self, db, monkeypatch):
        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "")
        self.limited_at(db, "my.greenhouse.io", 40)
        queued = browse_plan.enqueue(
            db, ["https://www.linkedin.com/jobs/search/?keywords=go"],
            priority=browse_plan.PRIORITY_SWEEP,
        )
        assert queued == 1

    def test_the_depth_it_tolerated_is_remembered(self, db):
        self.limited_at(db, "my.greenhouse.io", 40)
        assert browse_plan.tolerated_passes(db, "my.greenhouse.io") == 40

    def test_the_shallowest_objection_wins(self, db):
        # Being wrong low costs a few cards on one visit. Being wrong high
        # costs the visit and a twenty-minute rest.
        self.limited_at(db, "my.greenhouse.io", 40)
        self.limited_at(db, "my.greenhouse.io", 25)
        assert browse_plan.tolerated_passes(db, "my.greenhouse.io") == 25

    def test_a_board_that_never_objected_has_no_ceiling(self, db):
        assert browse_plan.tolerated_passes(db, "www.linkedin.com") is None

    def test_the_next_visit_asks_for_less(self, db):
        url = "https://my.greenhouse.io/jobs/search?query=x"
        asked_before = browse_plan._scroll_passes(url, db)
        self.limited_at(db, "my.greenhouse.io", 40)
        asked_after = browse_plan._scroll_passes(url, db)
        assert asked_after < asked_before
        # Under the objection rather than exactly at it: the depth the limit
        # bites at moves around, so sitting on the last known edge finds it
        # again about half the time.
        assert asked_after < 40

    def test_it_never_asks_for_more_than_the_board_wanted(self, db):
        # A generous tolerance is not licence to exceed the board's own number.
        url = "https://www.linkedin.com/jobs/search/?keywords=go"
        self.limited_at(db, "www.linkedin.com", 10_000)
        assert browse_plan._scroll_passes(url, db) <= \
            browse_plan._scroll_passes(url)

    def test_the_depth_never_reaches_zero(self, db):
        url = "https://my.greenhouse.io/jobs/search?query=x"
        self.limited_at(db, "my.greenhouse.io", 1)
        assert browse_plan._scroll_passes(url, db) >= 1

    def test_a_board_that_objected_gets_a_pause_between_batches(self, db, monkeypatch):
        """
        Worth more than the shallower scroll. The limit is a rate, so a slower
        hand reaches further — where a smaller pass count only decides where to
        give up.
        """
        monkeypatch.setattr(settings, "BROWSE_SCROLL_PAUSE_SECONDS", 2)
        url = "https://my.greenhouse.io/jobs/search?query=x"
        assert browse_plan._scroll_pause_seconds(url, db) == 0
        self.limited_at(db, "my.greenhouse.io", 40)
        assert browse_plan._scroll_pause_seconds(url, db) == 2

    def test_a_board_that_never_objected_is_not_slowed(self, db):
        # Pausing everywhere would be depth given away for nothing.
        assert browse_plan._scroll_pause_seconds(
            "https://www.linkedin.com/jobs/search/?keywords=go", db) == 0

    def test_the_queued_task_carries_both_numbers(self, db, monkeypatch):
        from app.models.browser_task import BrowserTask

        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "")
        monkeypatch.setattr(settings, "BROWSE_RATELIMIT_REST_MINUTES", 1)
        self.limited_at(db, "my.greenhouse.io", 40, minutes_ago=5)

        browse_plan.enqueue(
            db, ["https://my.greenhouse.io/jobs/search?query=x"],
            priority=browse_plan.PRIORITY_SWEEP,
        )
        task = db.query(BrowserTask).filter(
            BrowserTask.kind == "browse_page").one()
        assert task.payload["scroll_passes"] < 40
        assert task.payload["scroll_pause_seconds"] == \
            settings.BROWSE_SCROLL_PAUSE_SECONDS

    def test_the_panel_names_it_without_asking_for_work(self, db):
        # It needs nothing from the user but time, unlike a paused or a
        # challenged host — so it is reported separately from both.
        self.limited_at(db, "my.greenhouse.io", 40)
        status = browse_plan.status(db)
        assert status["resting"] == ["my.greenhouse.io"]
        assert status["blocked"] == []
        assert status["paused"] == []

    def test_the_page_renders_while_a_board_rests(self, db, monkeypatch, client):
        monkeypatch.setattr(settings, "AGENT_TOKEN", "t")
        self.limited_at(db, "my.greenhouse.io", 40)
        page = client.get("/runs")
        assert page.status_code == 200
        assert "my.greenhouse.io" in page.text


class TestRecordingARateLimit:
    def finished(self, db, **result):
        from app.models.browser_task import BrowserTask
        from app.services.agent_work import _ingest_browse_page

        url = "https://my.greenhouse.io/jobs/search?query=x"
        task = BrowserTask(
            kind="browse_page", status="done", agent_id="a",
            payload={"url": url, "purpose": "harvest"},
            result={"final_url": url, "signed_in": True, **result},
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        db.add(task)
        db.commit()
        _ingest_browse_page(db, task)
        return task

    def latest(self, db):
        from app.models.agent_event import AgentEvent

        return (
            db.query(AgentEvent)
            .filter(AgentEvent.kind == "browse")
            .order_by(AgentEvent.created_at.desc())
            .first()
        )

    def test_the_limit_and_the_depth_are_both_kept(self, db):
        self.finished(db, rate_limited=True, passes_done=40)
        summary = self.latest(db).summary
        assert summary["rate_limited"] is True
        assert summary["passes_done"] == 40

    def test_a_rate_limited_visit_is_still_a_successful_one(self, db):
        """
        Unlike a human check. The cards harvested before the limit are real and
        already stored, so marking the visit failed would tell the harvest
        health panel a working reader had broken.
        """
        self.finished(db, rate_limited=True, passes_done=40)
        assert self.latest(db).ok is True

    def test_an_old_extension_reporting_neither_still_works(self, db):
        self.finished(db)
        summary = self.latest(db).summary
        assert summary["rate_limited"] is False
        assert summary["passes_done"] == 0


class TestBoardsPaginateThreeDifferentWays:
    """
    A board's depth comes from one of three places, and using the wrong one
    harvests page one forever while every number on the panel looks healthy.

    * **A URL** — LinkedIn's `start=25`. Queue the next page as its own visit.
    * **A scroll** — Greenhouse's board and JobRight. There is no page two, so
      the scroll *is* the pagination and the pass count is the depth.
    * **A click** — Hiring Cafe. Numbered buttons at the bottom and one address
      for all of them, so there is no parameter to append and scrolling stops
      at the bottom of page one.

    The third had no mechanism at all. Hiring Cafe was configured as an entry
    page with the default scroll, which reaches the bottom of the first page,
    finds nothing more, and closes — reporting a perfectly ordinary visit.
    """

    def test_linkedin_pages_by_url(self, db):
        board = browse_plan.BOARDS_BY_KEY["linkedin"]
        pages = board.pages("https://www.linkedin.com/jobs/search/?keywords=go", 3)
        assert len(pages) == 3
        assert "start=25" in pages[1]

    def test_a_scrolling_board_gets_no_url_pages(self, db):
        board = browse_plan.BOARDS_BY_KEY["greenhouse"]
        assert board.pages("https://my.greenhouse.io/jobs/search", 5) == [
            "https://my.greenhouse.io/jobs/search"]

    def test_jobright_scrolls_deep_rather_than_taking_the_default(self, db):
        """
        It was silently taking the 25 meant for a board whose depth comes from
        queueing page two. JobRight has no page two — it is infinite scroll
        with lazy loading, so 25 passes is the first screenful and a closed tab.
        """
        deep = browse_plan._scroll_passes("https://jobright.ai/jobs/recommend")
        default = int(settings.BROWSE_SCROLL_PASSES)
        assert deep > default

    def test_hiring_cafe_is_told_to_click_through(self, db):
        assert browse_plan._max_pages("https://hiring.cafe/") > 1

    def test_a_scrolling_board_is_not_told_to_click(self, db):
        # One everywhere else, so the extension's behaviour on every board that
        # already worked is exactly what it was.
        assert browse_plan._max_pages("https://my.greenhouse.io/jobs/search") == 1
        assert browse_plan._max_pages(
            "https://www.linkedin.com/jobs/search/?keywords=go") == 1
        assert browse_plan._max_pages("https://example.com/whatever") == 1

    def test_hiring_cafe_scrolls_only_enough_to_reach_the_controls(self, db):
        # Its depth comes from the pages. Scrolling hard on each one would be
        # time spent for nothing, and the controls are at the bottom of a short
        # page anyway.
        passes = browse_plan._scroll_passes("https://hiring.cafe/")
        assert passes < browse_plan._scroll_passes("https://jobright.ai/jobs/recommend")

    def test_the_queued_task_says_how_many_pages_to_click(self, db, monkeypatch):
        from app.models.browser_task import BrowserTask

        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "")
        browse_plan.enqueue(db, ["https://hiring.cafe/"],
                            priority=browse_plan.PRIORITY_REQUESTED)
        task = db.query(BrowserTask).filter(
            BrowserTask.kind == "browse_page").one()
        assert task.payload["max_pages"] > 1

    def test_every_queued_task_carries_the_key(self, db, monkeypatch):
        # Absent would mean the extension falls back to its own default, which
        # is the kind of split-brain default that drifts apart silently.
        from app.models.browser_task import BrowserTask

        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "")
        browse_plan.enqueue(db, ["https://my.greenhouse.io/jobs/search"],
                            priority=browse_plan.PRIORITY_REQUESTED)
        task = db.query(BrowserTask).filter(
            BrowserTask.kind == "browse_page").one()
        assert task.payload["max_pages"] == 1


class TestAPaginatedBoardThatOnlyReachedPageOne:
    """
    The failure this has to stay visible for. If Hiring Cafe redesigns its
    pagination, or the guess at its markup is wrong, the visit still scrolls
    fine and still harvests rows — it just harvests the same twenty every time.
    Nothing in the scroll numbers can show that.
    """

    def finished(self, db, url, **result):
        from app.models.browser_task import BrowserTask
        from app.services.agent_work import _ingest_browse_page

        task = BrowserTask(
            kind="browse_page", status="done", agent_id="a",
            payload={"url": url, "purpose": "harvest"},
            result={"final_url": url, "signed_in": True, **result},
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        db.add(task)
        db.commit()
        _ingest_browse_page(db, task)
        return task

    def latest(self, db):
        from app.models.agent_event import AgentEvent

        return (
            db.query(AgentEvent)
            .filter(AgentEvent.kind == "browse")
            .order_by(AgentEvent.created_at.desc())
            .first()
        )

    def test_the_pages_reached_are_recorded(self, db):
        self.finished(db, "https://hiring.cafe/", pages_done=7, passes_done=30)
        assert self.latest(db).summary["pages_done"] == 7

    def test_one_page_from_a_paginated_board_is_logged(self, db, caplog):
        import logging

        with caplog.at_level(logging.WARNING):
            self.finished(db, "https://hiring.cafe/", pages_done=1, passes_done=8)
        assert "next-page control was not found" in caplog.text

    def test_one_page_from_a_scrolling_board_is_not(self, db, caplog):
        # Greenhouse reaching one "page" is simply what a scrolling board does.
        import logging

        with caplog.at_level(logging.WARNING):
            self.finished(db, "https://my.greenhouse.io/jobs/search",
                          pages_done=1, passes_done=180)
        assert "next-page control" not in caplog.text

    def test_a_rate_limit_is_not_blamed_on_the_control(self, db, caplog):
        """
        Being stopped after one page by a rate limit is a different diagnosis
        with a different fix, and it already reports itself. Two warnings for
        one event would send the reader after the wrong one.
        """
        import logging

        with caplog.at_level(logging.WARNING):
            self.finished(db, "https://hiring.cafe/", pages_done=1,
                          passes_done=4, rate_limited=True)
        assert "next-page control" not in caplog.text

    def test_an_old_extension_reporting_nothing_is_not_an_error(self, db):
        self.finished(db, "https://hiring.cafe/")
        assert self.latest(db).summary["pages_done"] == 0


class TestHandshakePagesByUrl:
    """
    Its own URL gave the scheme away: `/job-search?page=1&per_page=25`. The
    board had been added pointing at `/stu/postings` with no pagination at all,
    so it opened one page of somebody's saved postings and stopped.

    The `page_size`/`page_base` split is what stops the obvious mistake here.
    `per_page=25` is tempting to read as the step, but `page` is an ordinal —
    stepping by 25 would ask for page 26 next and skip the twenty-four in
    between, while looking exactly like working pagination.
    """

    def test_it_walks_ordinal_pages(self, db):
        board = browse_plan.BOARDS_BY_KEY["handshake"]
        pages = board.pages(board.entries[0], 4)
        assert pages[1].endswith("page=2")
        assert pages[2].endswith("page=3")
        assert pages[3].endswith("page=4")

    def test_the_first_page_carries_no_page_parameter(self, db):
        # Carrying `page=1` in the entry would put two of them in every later
        # URL, and which one a board honours is anyone's guess.
        board = browse_plan.BOARDS_BY_KEY["handshake"]
        assert "page=" not in board.entries[0].split("per_page=")[0]

    def test_it_asks_the_search_rather_than_the_saved_list(self, db):
        board = browse_plan.BOARDS_BY_KEY["handshake"]
        assert "/job-search" in board.entries[0]

    def test_a_crawl_queues_more_than_one_page(self, db, monkeypatch):
        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "")
        monkeypatch.setattr(settings, "BROWSE_SEARCH_PAGES", 3)
        browse_plan.crawl_searches(db, PROFILE, board="handshake")
        urls = [u for u in queued_urls(db) if "joinhandshake" in u]
        assert len(urls) >= 3


class TestAChallengeOnALinkIsReportedToo:
    """
    Jooble's `away` redirects are `resolve_link` tasks, not browse tasks — and
    only the browse path read the challenge off a result. So the window opened,
    waited ninety seconds, timed out, and told the server nothing: no event, no
    backoff, nothing on the panel. From outside it was a window that flashed
    and a system that never mentioned it.
    """

    def finished(self, db, challenge, url="https://jooble.org/away/1"):
        from app.models.browser_task import BrowserTask
        from app.services.agent_work import _ingest_resolve_link

        task = BrowserTask(
            kind="resolve_link", status="done", agent_id="a",
            payload={"url": url},
            result={"final_url": url, "html": "", "challenge": challenge},
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        db.add(task)
        db.commit()
        _ingest_resolve_link(db, task)
        return task

    def latest(self, db):
        from app.models.agent_event import AgentEvent

        return (
            db.query(AgentEvent)
            .filter(AgentEvent.kind == "browse")
            .order_by(AgentEvent.created_at.desc())
            .first()
        )

    def test_an_unanswered_check_is_recorded(self, db):
        self.finished(db, "timeout")
        assert self.latest(db) is not None
        assert self.latest(db).summary["challenge"] == "timeout"

    def test_it_backs_the_host_off(self, db):
        # Recorded as a `browse` event on purpose: `blocked_hosts` reads that
        # one kind, and a second shape would leave a host blocked on one path
        # and merrily retried on the other.
        self.finished(db, "timeout")
        assert "jooble.org" in browse_plan.blocked_hosts(db)

    def test_passing_the_check_is_not_held_against_it(self, db):
        self.finished(db, "passed")
        assert browse_plan.blocked_hosts(db) == set()

    def test_a_link_that_never_asked_records_nothing(self, db):
        self.finished(db, "")
        assert self.latest(db) is None

    def test_the_panel_names_it(self, db):
        self.finished(db, "timeout")
        assert browse_plan.status(db)["blocked"] == ["jooble.org"]

    def test_enrichment_stops_queueing_that_host(self, db, monkeypatch):
        # The point of the backoff: without it every thin Jooble job keeps
        # queueing a visit that cannot succeed.
        from app.services import enrichment

        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "")
        self.finished(db, "timeout")
        job = thin_job(db, "https://jooble.org/away/77", source="jooble")
        queued, deferred = enrichment.plan_browser_queue(db, [job])
        assert queued == []
        assert [j.id for j in deferred] == [job.id]


class TestPassingTheCheckYourself:
    """
    The thing that was actually missing, and no amount of better detection was
    going to supply it.

    Two structural faults, either of which alone made this unwinnable:

    *The proof was thrown away.* Passing a Cloudflare check sets a clearance
    cookie, and that cookie is the only evidence you passed. Link resolution
    fetched with `credentials: "omit"`, so every request arrived as a first-time
    visitor and was challenged again — solve it perfectly and the next link is
    challenged identically, forever.

    *There was nowhere to solve it.* Every tab this system opens is minimized
    and closed again, which is right for a crawl and exactly wrong for the one
    case where a person has to see the page and click something.
    """

    def event(self, db, host, outcome, minutes_ago=0):
        from app.models.agent_event import AgentEvent

        row = AgentEvent(
            kind="browse", host=host, ok=(outcome == "passed"),
            summary={"purpose": "resolve", "challenge": outcome},
        )
        db.add(row)
        db.flush()
        if minutes_ago:
            row.created_at = datetime.now(timezone.utc) - timedelta(
                minutes=minutes_ago)
        db.commit()
        return row

    def test_passing_clears_an_earlier_block(self, db):
        """
        Without this the backoff outlived the thing it was waiting for: you
        would go and pass the check, and the host would stay untouched for the
        rest of the day anyway — which reads as the click having achieved
        nothing, which is exactly the complaint.
        """
        self.event(db, "jooble.org", "timeout", minutes_ago=30)
        assert "jooble.org" in browse_plan.blocked_hosts(db)

        self.event(db, "jooble.org", "passed")
        assert "jooble.org" not in browse_plan.blocked_hosts(db)

    def test_a_later_block_still_counts(self, db):
        # Clearance expires. Passing once is not a permanent exemption.
        self.event(db, "jooble.org", "passed", minutes_ago=60)
        self.event(db, "jooble.org", "timeout")
        assert "jooble.org" in browse_plan.blocked_hosts(db)

    def test_one_host_passing_does_not_clear_another(self, db):
        self.event(db, "jooble.org", "timeout")
        self.event(db, "indeed.com", "timeout")
        self.event(db, "jooble.org", "passed")
        assert browse_plan.blocked_hosts(db) == {"indeed.com"}

    def test_the_button_queues_a_visible_tab(self, db, client, monkeypatch):
        from app.models.browser_task import BrowserTask

        monkeypatch.setattr(settings, "AGENT_TOKEN", "t")
        response = client.post("/runs/agent/pass-check",
                               data={"host": "jooble.org"})
        assert response.status_code == 200

        task = db.query(BrowserTask).filter(
            BrowserTask.kind == "pass_check").one()
        assert task.payload["url"] == "https://jooble.org/"

    def test_it_goes_to_the_front_of_the_queue(self, db, client, monkeypatch):
        # You pressed it and are about to go and look at the tab it opens.
        from app.models.browser_task import BrowserTask

        monkeypatch.setattr(settings, "AGENT_TOKEN", "t")
        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "")
        browse_plan.enqueue(db, ["https://my.greenhouse.io/jobs/search"],
                            priority=browse_plan.PRIORITY_SWEEP)
        client.post("/runs/agent/pass-check", data={"host": "jooble.org"})

        first = browser_tasks.lease(db, None, agent_id="a", limit=1)[0]
        assert first.kind == "pass_check"

    def test_the_page_offers_it_for_a_blocked_host(self, db, client, monkeypatch):
        monkeypatch.setattr(settings, "AGENT_TOKEN", "t")
        self.event(db, "jooble.org", "timeout")
        page = client.get("/runs").text
        assert "Let me pass jooble.org" in page

    def test_a_host_with_no_block_is_not_offered(self, db, client, monkeypatch):
        monkeypatch.setattr(settings, "AGENT_TOKEN", "t")
        assert "Let me pass" not in client.get("/runs").text

    def test_a_missing_host_is_refused_rather_than_queued(self, db, client, monkeypatch):
        from app.models.browser_task import BrowserTask

        monkeypatch.setattr(settings, "AGENT_TOKEN", "t")
        client.post("/runs/agent/pass-check", data={"host": "  "})
        assert db.query(BrowserTask).filter(
            BrowserTask.kind == "pass_check").count() == 0

    def test_the_kind_is_one_the_queue_accepts(self, db):
        # It is leased and reported like any other task, so it has to be in the
        # closed set rather than passed through as a string.
        from app.models.browser_task import TASK_KINDS

        assert "pass_check" in TASK_KINDS


class TestAHostThatKeepsTurningUsAwayIsLeftAloneForLonger:
    """
    A flat backoff is not a backoff against a host that has decided to check
    everyone. Jooble proved it: the pause lifted every twenty-four hours,
    another sixty pages were queued, every one of them raised a check nobody
    got past, and it repeated — 448 visits in a week that reached no job, while
    Handshake and JobRight got sixteen and ten between them.
    """

    def _challenge(self, db, host, when, outcome="timeout"):
        from app.models.agent_event import AgentEvent

        event = AgentEvent(
            kind="browse", host=host, ok=False,
            summary={"challenge": outcome, "signed_in": True},
        )
        db.add(event)
        db.flush()
        # created_at has a server default, so the date being tested has to be
        # written over it rather than passed in.
        event.created_at = when
        db.commit()

    def test_one_bad_day_is_the_base_backoff(self, db):
        from app.services import browse_plan

        now = datetime.now(timezone.utc)
        self._challenge(db, "jooble.org", now - timedelta(hours=2))
        assert "jooble.org" in browse_plan.blocked_hosts(db)

        self._challenge(db, "other.example", now - timedelta(hours=30))
        assert "other.example" not in browse_plan.blocked_hosts(db)

    def test_sixty_refusals_in_one_evening_count_as_one(self, db):
        # Otherwise a single bad night escalates to a year, and a host that was
        # briefly unhappy is never tried again.
        from app.services import browse_plan

        now = datetime.now(timezone.utc)
        for minute in range(60):
            self._challenge(db, "jooble.org", now - timedelta(hours=30, minutes=minute))

        assert "jooble.org" not in browse_plan.blocked_hosts(db), (
            "one day of refusals earns one day of backoff, however many pages"
        )

    def test_each_further_day_doubles_the_pause(self, db):
        from app.services import browse_plan

        now = datetime.now(timezone.utc)
        # Three separate days of being turned away: 24h, then 48, then 96.
        for days in (5, 4, 3):
            self._challenge(db, "jooble.org", now - timedelta(days=days))
        self._challenge(db, "jooble.org", now - timedelta(hours=50))

        # Four strikes is a 192-hour pause, and the last refusal was 50 hours
        # ago, so it stays blocked where a flat 24 would have let it go.
        assert "jooble.org" in browse_plan.blocked_hosts(db)

    def test_passing_the_check_wipes_the_record(self, db):
        # The click has to be worth something. Passing is precisely the event
        # that invalidates the block, including everything it had accumulated.
        from app.services import browse_plan

        now = datetime.now(timezone.utc)
        for days in (5, 4, 3, 2):
            self._challenge(db, "jooble.org", now - timedelta(days=days))
        self._challenge(db, "jooble.org", now - timedelta(hours=1), outcome="passed")

        assert "jooble.org" not in browse_plan.blocked_hosts(db)

    def test_the_pause_is_capped_rather_than_doubling_forever(self, db):
        from app.services import browse_plan
        from app.config import settings

        now = datetime.now(timezone.utc)
        for days in range(30, 1, -1):
            self._challenge(db, "jooble.org", now - timedelta(days=days))

        # Thirty strikes would be centuries of doubling; the ceiling is what
        # keeps a host that might relent from being written off forever.
        assert browse_plan._backoff_hours(30) == \
            settings.BROWSE_CHALLENGE_MAX_BACKOFF_HOURS
