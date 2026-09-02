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


def linkedin_urls(urls):
    return [url for url in urls if "linkedin.com" in url]


class TestWhichSearchesItWalks:
    def test_one_per_role_and_location(self, db):
        # Depth pinned: this is about the role x location cross, and letting
        # the page count in would make it a test of two things at once.
        urls = linkedin_urls(browse_plan.search_urls(PROFILE, depth=1))
        assert len(urls) == 4
        assert all(url.startswith("https://www.linkedin.com/jobs/search/") for url in urls)

    def test_the_role_and_location_are_encoded(self, db):
        urls = linkedin_urls(browse_plan.search_urls(
            {"target_roles": ["Site Reliability Engineer"],
             "target_locations": ["New York, NY"]}
        ))
        assert "keywords=Site+Reliability+Engineer" in urls[0]
        assert "location=New+York%2C+NY" in urls[0]

    def test_it_asks_for_the_last_week_only(self, db):
        # Without this the budget goes on re-reading postings from months ago
        # that are already stored, which is the expensive way to harvest
        # nothing.
        assert "f_TPR=r604800" in linkedin_urls(browse_plan.search_urls(PROFILE))[0]

    def test_no_target_roles_means_no_searches_on_a_search_board(self, db):
        assert linkedin_urls(browse_plan.search_urls({"target_locations": ["London"]})) == []
        assert linkedin_urls(browse_plan.search_urls(None)) == []

    def test_a_profile_with_no_locations_still_searches(self, db):
        assert len(linkedin_urls(browse_plan.search_urls(
            {"target_roles": ["Backend Engineer"]}, depth=1
        ))) == 1


class TestItWalksMoreThanTheFirstPage:
    """
    A search page is about twenty-five cards. Stopping there is what made a
    "crawl" look like it was discovering nothing — the whole difference between
    a peek and a sweep is depth, and depth costs only more queued visits.
    """

    def test_a_search_becomes_several_result_pages(self, db, monkeypatch):
        monkeypatch.setattr(settings, "BROWSE_SEARCH_PAGES", 4)
        urls = linkedin_urls(browse_plan.search_urls(
            {"target_roles": ["Backend Engineer"], "target_locations": ["London"]}
        ))
        assert len(urls) == 4

    def test_the_pages_step_by_the_boards_own_size(self, db, monkeypatch):
        monkeypatch.setattr(settings, "BROWSE_SEARCH_PAGES", 4)
        urls = linkedin_urls(browse_plan.search_urls(
            {"target_roles": ["Backend Engineer"], "target_locations": ["London"]}
        ))
        assert "start=25" in urls[1]
        assert "start=50" in urls[2]
        assert "start=75" in urls[3]

    def test_the_first_page_carries_no_page_parameter(self, db, monkeypatch):
        monkeypatch.setattr(settings, "BROWSE_SEARCH_PAGES", 3)
        first = linkedin_urls(browse_plan.search_urls(
            {"target_roles": ["Backend Engineer"], "target_locations": ["London"]}
        ))[0]
        assert "start=" not in first

    def test_an_ordinal_board_starts_its_second_page_at_two(self, db):
        # Boards count two ways — an offset in results, or an ordinal page.
        # Assuming the first turns page two of an ordinal board back into page
        # one, so every search fetches the first page twice and never reaches
        # the fourth.
        google = browse_plan.BOARDS_BY_KEY["google"]
        pages = google.pages("https://x/jobs?q=a", 4)

        assert "page=2" in pages[1]
        assert "page=3" in pages[2]
        assert "page=1" not in "".join(pages)

    def test_a_board_with_no_paging_scheme_stays_one_page(self, db):
        board = browse_plan.Board("x", "x.com", "X", search="https://x.com/?q={q}&l={loc}")
        assert board.pages("https://x.com/?q=a&l=b", 5) == ["https://x.com/?q=a&l=b"]

    def test_depth_of_one_is_the_page_itself(self, db):
        board = browse_plan.BOARDS_BY_KEY["linkedin"]
        assert board.pages("https://x/?q=a", 1) == ["https://x/?q=a"]

    def test_the_run_cap_still_holds(self, db, monkeypatch):
        # Depth multiplies the URL count, so the ceiling on a run matters more
        # than it did — this must not become a way to queue six hundred visits.
        monkeypatch.setattr(settings, "BROWSE_SEARCH_PAGES", 10)
        monkeypatch.setattr(settings, "BROWSE_MAX_QUEUED", 15)

        assert browse_plan.crawl_searches(db, PROFILE)["queued"] == 15


class TestCompanyCareersBoards:
    def test_amazon_and_google_are_crawled(self, db):
        urls = browse_plan.search_urls(PROFILE)
        assert any("amazon.jobs" in url for url in urls)
        assert any("google.com/about/careers" in url for url in urls)

    def test_the_role_reaches_their_search_box(self, db):
        urls = browse_plan.search_urls(
            {"target_roles": ["Backend Engineer"], "target_locations": ["London"]}
        )
        amazon = next(url for url in urls if "amazon.jobs" in url)
        assert "base_query=Backend+Engineer" in amazon

    def test_google_is_scoped_to_the_careers_path(self, db):
        # "Read everything you do on Google" is not the permission this needs,
        # and would be the most alarming line in the install prompt.
        source = open("extension/sites.js").read()
        assert "https://www.google.com/about/careers/*" in source
        assert '"https://www.google.com/*"' not in source


class TestBoardsWhoseSearchIsNotAUrl:
    """
    JobRight and its peers render results from an internal API. Their query
    parameters are nobody's business but that app's, so guessing at one
    produces a crawl that opens error pages very politely. They get their own
    entry pages instead, and the paste box covers the rest.
    """

    def test_jobright_is_walked_by_its_own_feed(self, db):
        urls = browse_plan.search_urls(PROFILE)
        assert any("jobright.ai/jobs/recommend" in url for url in urls)

    def test_its_pages_are_queued_even_with_no_target_roles(self, db):
        # A recommendations feed is already filtered to the account browsing
        # it, so it needs no query at all — and going quiet because the profile
        # has no roles would be wrong.
        urls = browse_plan.search_urls({})
        assert any("jobright.ai" in url for url in urls)

    def test_no_url_is_invented_for_it(self, db):
        # The failure this guards against: a plausible-looking search URL with
        # made-up parameters, which 404s politely sixty times.
        for url in browse_plan.search_urls(PROFILE):
            if "jobright.ai" in url:
                assert "keywords=" not in url and "?q=" not in url

    def test_one_board_can_be_crawled_on_its_own(self, db):
        outcome = browse_plan.crawl_searches(db, PROFILE, board="jobright")
        assert outcome["queued"] == 2
        assert all("jobright.ai" in url for url in queued(db))

    def test_an_unknown_board_name_falls_back_to_all_of_them(self, db):
        urls = browse_plan.search_urls(PROFILE)
        outcome = browse_plan.crawl_searches(db, PROFILE, board="nonsense")
        assert outcome["candidates"] == len(urls)

    def test_every_board_is_one_the_extension_may_read(self, db):
        # A board queued here but not in the extension's site list is a crawl
        # that opens pages with no interceptor on them: real traffic through a
        # logged-in session, harvesting nothing.
        import re

        source = open("extension/sites.js").read()
        # Any match pattern, not only the bare `host/*` shape: a site scoped to
        # a path — Google Careers is — is still a site the extension may read,
        # and reading only the bare form would report it as uncovered.
        allowed = {
            re.sub(r"^\*\.", "", host)
            for host in re.findall(r'"https://([^/"]+)/', source)
        }
        for board in browse_plan.BOARDS:
            assert any(
                board.host == host or board.host.endswith(f".{host}")
                or host.endswith(f".{board.host}")
                for host in allowed
            ), f"{board.host} is crawled but not harvested"


class TestABoardThatHidesItsListUntilYouSearch:
    """
    Tsenta shows about five results on load and only fetches the real list once
    its search has been submitted — with nothing typed in, because it matches
    against the profile rather than a keyword.

    That is a fourth way a crawl can come back nearly empty, and from the
    outside it is indistinguishable from the other three: the page loads, a few
    results render, and the scroll reaches the bottom of those few and reports
    the list finished. Nothing about it looks like a fault.
    """

    def test_it_is_queued_from_its_feed(self, db):
        urls = browse_plan.search_urls(PROFILE)
        assert any("tsenta.com" in url for url in urls)

    def test_it_needs_no_keyword(self, db):
        # An aggregator already matching against the account, so an empty
        # profile must not silence it.
        assert any("tsenta.com" in url for url in browse_plan.search_urls({}))

    def test_the_task_asks_for_the_search_to_be_submitted(self, db):
        from app.models.browser_task import BrowserTask

        browse_plan.crawl_searches(db, PROFILE, board="tsenta")
        task = db.query(BrowserTask).first()
        assert task.payload["submit_search"] is True

    def test_no_other_board_is_asked_to(self, db):
        # Pressing Enter re-runs a search that already ran, and on a board that
        # did not need it that costs a round trip and the scroll position.
        for url in ("https://www.linkedin.com/jobs/search?keywords=x",
                    "https://jobright.ai/jobs/recommend",
                    "https://my.greenhouse.io/jobs/search?query=x"):
            assert browse_plan._submit_search(url) is False

    def test_a_url_belonging_to_no_board_does_not_either(self, db):
        assert browse_plan._submit_search("https://example.test/jobs") is False

    def test_it_scrolls_deep_because_that_is_its_pagination(self, db):
        # Infinite scroll with no page-two URL, so the scroll loop is the only
        # way past the first screenful.
        board = browse_plan.BOARDS_BY_KEY["tsenta"]
        assert board.page_param is None
        assert board.scroll_passes >= 100

    def test_its_api_lives_on_another_domain_and_is_still_its_own(self):
        """
        The board is served by `api.autojobs.me`, and a harvested payload is
        filed under the host it came from. Unlisted, its jobs would be counted
        as LinkedIn's — the fallback — and its samples filtered off the panel
        as belonging to no board of ours.
        """
        from app.services.harvest import source_for_url

        api = ("https://api.autojobs.me/api/v1/jobs/recommendations"
               "?limit=20&page=5")
        assert source_for_url(api) == "tsenta_harvest"
        assert source_for_url("https://dashboard.tsenta.com/dashboard/"
                              "recommendations") == "tsenta_harvest"

    def test_its_samples_are_not_swept_up_as_telemetry(self, db):
        # The cleanup added for ad-tech hosts would otherwise delete exactly
        # the payloads this board exists to collect.
        from app.services import harvest_samples

        assert harvest_samples._related(
            "api.autojobs.me", harvest_samples.worth_learning(db))


class TestPastedUrls:
    def test_it_queues_what_was_pasted(self, db):
        outcome = browse_plan.crawl_urls(db, """
            https://jobright.ai/jobs/search?x=1
            https://hiring.cafe/?q=backend
        """)
        assert outcome["queued"] == 2

    def test_it_accepts_a_comma_separated_list_too(self, db):
        assert browse_plan.crawl_urls(
            db, "https://a.example/1, https://b.example/2"
        )["queued"] == 2

    def test_it_ignores_anything_that_is_not_a_link(self, db):
        outcome = browse_plan.crawl_urls(db, "notes to self\nhttps://a.example/1\n\n")
        assert outcome["queued"] == 1

    def test_duplicates_are_collapsed(self, db):
        outcome = browse_plan.crawl_urls(
            db, "https://a.example/1\nhttps://a.example/1"
        )
        assert outcome["queued"] == 1

    def test_pasting_nothing_queues_nothing(self, db):
        assert browse_plan.crawl_urls(db, "")["queued"] == 0
        assert browse_plan.crawl_urls(db, "   \n  ")["queued"] == 0

    def test_a_pasted_run_is_capped_like_any_other(self, db, monkeypatch):
        monkeypatch.setattr(settings, "BROWSE_MAX_QUEUED", 2)
        pasted = "\n".join(f"https://a.example/{n}" for n in range(10))
        assert browse_plan.crawl_urls(db, pasted)["queued"] == 2


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

    def test_jobs_on_a_board_nobody_can_browse_are_not_opened(self, db):
        # Greenhouse has a public API that already returns the description, so
        # spending a paced browser visit on one buys nothing.
        make_job(db, source="greenhouse", url="https://boards.greenhouse.io/a/jobs/1",
                 source_job_id="55", description="thin")
        assert browse_plan.posting_urls(db) == []

    def test_a_thin_job_on_another_browsable_board_is_opened(self, db):
        # Selected by the job's own URL rather than by source name: a posting
        # is browsable if a browser can reach it, which is a fact about the
        # link and not about which adapter found it.
        make_job(db, source="jobright_harvest", source_job_id="abc123",
                 url="https://jobright.ai/jobs/info/xyz789", description="thin")
        assert browse_plan.posting_urls(db) == [
            "https://jobright.ai/jobs/info/xyz789"
        ]

    def test_another_board_keeps_its_own_url_shape(self, db):
        # Only LinkedIn's is rebuilt from an id. Nothing here should need to
        # know how any other site composes a link.
        make_job(db, source="jobright_harvest", source_job_id="4012345678",
                 url="https://jobright.ai/jobs/info/xyz789?ref=feed",
                 description="thin")
        assert browse_plan.posting_urls(db) == [
            "https://jobright.ai/jobs/info/xyz789"
        ]

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

    def test_a_page_browsed_recently_is_not_reopened_by_the_sweep(self, db):
        # Otherwise a nightly run re-reads the same hundred postings forever
        # and never reaches the ones behind them.
        #
        # Only the sweep: a button press means "do it again", and applying this
        # cooloff to one turned it into a control that queued nothing and said
        # nothing. See `test_browse_priority`.
        make_job(db, source_job_id="4012345678", description="thin")
        browse_plan.crawl_postings(db, priority=browse_plan.PRIORITY_SWEEP)
        task = db.query(BrowserTask).one()
        task.status = "done"
        db.commit()

        browse_plan.crawl_postings(db, priority=browse_plan.PRIORITY_SWEEP)
        assert len(queued(db)) == 1

    def test_it_tries_again_after_the_cooloff(self, db, monkeypatch):
        monkeypatch.setattr(settings, "BROWSE_RETRY_DAYS", 30)
        make_job(db, source_job_id="4012345678", description="thin")
        browse_plan.crawl_postings(db, priority=browse_plan.PRIORITY_SWEEP)
        task = db.query(BrowserTask).one()
        task.status = "done"
        task.created_at = datetime.now(timezone.utc) - timedelta(days=60)
        db.commit()

        browse_plan.crawl_postings(db, priority=browse_plan.PRIORITY_SWEEP)
        assert len(queued(db)) == 2

    def test_it_can_be_turned_off(self, db, monkeypatch):
        monkeypatch.setattr(settings, "BROWSE_ENABLED", False)
        make_job(db, description="thin")

        assert browse_plan.crawl_postings(db)["queued"] == 0
        assert queued(db) == []

    def test_only_the_unattended_sweep_goes_to_the_back(self, db):
        # This replaces a test that asserted browsing *never* jumps the queue,
        # on the grounds that link resolution and enrichment answer a question
        # something is waiting on while a crawl is a background sweep. Half
        # right: it is true of the scheduled pass and false of a button press,
        # and treating them the same put an explicit request behind hundreds of
        # background pages at twenty seconds each.
        make_job(db, description="thin")
        browse_plan.crawl_postings(db, priority=browse_plan.PRIORITY_SWEEP)

        assert db.query(BrowserTask).one().priority == browse_plan.PRIORITY_SWEEP
        assert browse_plan.PRIORITY_SWEEP < browse_plan.PRIORITY_ENRICHMENT


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
        assert "Crawl boards" in body

    def test_the_panel_says_it_is_slow_on_purpose(self, client, db):
        # A user who does not know the pacing is deliberate will read a slow
        # queue as a bug and go looking for the throttle.
        assert "slow on purpose" in client.get("/runs").text

    def test_pressing_fill_in_descriptions_queues_postings(self, client, db):
        make_job(db, source_job_id="4012345678", description="thin")
        client.post("/runs/agent/browse", data={"plan": "postings"})

        assert queued(db) == ["https://www.linkedin.com/jobs/view/4012345678/"]

    def test_pressing_crawl_boards_queues_every_board(self, client, db):
        from app.models.profile import Profile

        db.add(Profile(data=PROFILE))
        db.commit()
        client.post("/runs/agent/browse", data={"plan": "searches"})

        urls = queued(db)
        # Four searches, each walked several pages deep.
        assert len(linkedin_urls(urls)) > 4
        assert any("jobright.ai" in url for url in urls)

    def test_a_single_board_can_be_queued_from_the_panel(self, client, db):
        client.post("/runs/agent/browse",
                    data={"plan": "searches", "board": "jobright"})
        assert all("jobright.ai" in url for url in queued(db))

    def test_the_panel_offers_a_button_per_board(self, client, db):
        body = client.get("/runs").text
        assert "JobRight" in body
        assert '"board": "jobright"' in body

    def test_pasted_urls_can_be_queued_from_the_panel(self, client, db):
        client.post("/runs/agent/browse",
                    data={"plan": "urls",
                          "urls": "https://jobright.ai/jobs/search?keyword=backend"})
        assert queued(db) == ["https://jobright.ai/jobs/search?keyword=backend"]

    def test_the_panel_offers_the_paste_box(self, client, db):
        body = client.get("/runs").text
        assert 'name="urls"' in body
        assert "paste your own urls" in body.lower()

    def test_a_failure_does_not_take_the_page_down(self, client, db, monkeypatch):
        from app.services import browse_plan as module

        monkeypatch.setattr(module, "crawl_postings",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        assert client.post("/runs/agent/browse", data={"plan": "postings"}).status_code == 200
