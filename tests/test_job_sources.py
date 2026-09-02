import re
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from app.services.sources import linkedin as linkedin_module


# ---------------------------------------------------------------------------
# Adzuna adapter
# ---------------------------------------------------------------------------

class TestAdzunaAdapter:
    def _mock_response(self, jobs_data: list[dict]) -> MagicMock:
        resp = MagicMock()
        resp.json.return_value = {"results": jobs_data}
        resp.raise_for_status = MagicMock()
        return resp

    def test_returns_standard_dicts(self):
        from app.services.sources.adzuna import fetch
        raw = [{
            "id": "AZ123",
            "title": "Senior Python Engineer",
            "company": {"display_name": "Stripe"},
            "location": {"display_name": "New York, NY"},
            "redirect_url": "https://adzuna.com/jobs/AZ123",
            "description": "Build payment systems.",
            "contract_type": "permanent",
        }]
        with patch("httpx.get", return_value=self._mock_response(raw)):
            results = fetch(app_id="ID", app_key="KEY", query="Python", location="New York")
        assert len(results) == 1
        job = results[0]
        assert job["source"] == "adzuna"
        assert job["source_job_id"] == "AZ123"
        assert job["title"] == "Senior Python Engineer"
        assert job["company"] == "Stripe"
        assert job["location"] == "New York, NY"
        assert job["url"] == "https://adzuna.com/jobs/AZ123"
        assert job["experience_level"] == "senior"

    def test_remote_detection_from_location(self):
        from app.services.sources.adzuna import fetch
        raw = [{
            "id": "AZ124",
            "title": "Backend Engineer",
            "company": {"display_name": "Acme"},
            "location": {"display_name": "Remote"},
            "redirect_url": "https://adzuna.com/jobs/AZ124",
            "description": "Remote role.",
            "contract_type": "permanent",
        }]
        with patch("httpx.get", return_value=self._mock_response(raw)):
            results = fetch(app_id="ID", app_key="KEY", query="Engineer", location="Remote")
        assert results[0]["is_remote"] is True

    def test_empty_results(self):
        from app.services.sources.adzuna import fetch
        with patch("httpx.get", return_value=self._mock_response([])):
            results = fetch(app_id="ID", app_key="KEY", query="Python", location="NYC")
        assert results == []

    def test_http_error_returns_empty(self):
        from app.services.sources.adzuna import fetch
        import httpx
        with patch("httpx.get", side_effect=httpx.HTTPError("timeout")):
            results = fetch(app_id="ID", app_key="KEY", query="Python", location="NYC")
        assert results == []


# ---------------------------------------------------------------------------
# JSearch adapter
# ---------------------------------------------------------------------------

class TestJSearchAdapter:
    def _mock_response(self, jobs_data: list[dict]) -> MagicMock:
        resp = MagicMock()
        resp.json.return_value = {"data": jobs_data}
        resp.raise_for_status = MagicMock()
        return resp

    def test_returns_standard_dicts(self):
        from app.services.sources.jsearch import fetch
        raw = [{
            "job_id": "JS999",
            "job_title": "Backend Engineer",
            "employer_name": "Airbnb",
            "job_city": "San Francisco",
            "job_state": "CA",
            "job_country": "US",
            "job_is_remote": False,
            "job_apply_link": "https://careers.airbnb.com/job/1",
            "job_description": "Build scalable APIs.",
            "job_employment_type": "FULLTIME",
        }]
        with patch("httpx.get", return_value=self._mock_response(raw)):
            results = fetch(api_key="KEY", query="Backend Engineer", location="San Francisco")
        assert len(results) == 1
        job = results[0]
        assert job["source"] == "jsearch"
        assert job["source_job_id"] == "JS999"
        assert job["company"] == "Airbnb"
        assert job["is_remote"] is False
        assert job["url"] == "https://careers.airbnb.com/job/1"

    def test_remote_flag_from_api(self):
        from app.services.sources.jsearch import fetch
        raw = [{
            "job_id": "JS1000",
            "job_title": "SWE",
            "employer_name": "Co",
            "job_city": "",
            "job_state": "",
            "job_country": "US",
            "job_is_remote": True,
            "job_apply_link": "https://co.com/job",
            "job_description": "Remote role.",
            "job_employment_type": "FULLTIME",
        }]
        with patch("httpx.get", return_value=self._mock_response(raw)):
            results = fetch(api_key="KEY", query="SWE", location="Remote")
        assert results[0]["is_remote"] is True

    def test_http_error_returns_empty(self):
        from app.services.sources.jsearch import fetch
        import httpx
        with patch("httpx.get", side_effect=httpx.HTTPError("timeout")):
            results = fetch(api_key="KEY", query="SWE", location="NYC")
        assert results == []


# ---------------------------------------------------------------------------
# Greenhouse adapter
# ---------------------------------------------------------------------------

class TestGreenhouseAdapter:
    def _mock_response(self, jobs_data: list[dict]) -> MagicMock:
        resp = MagicMock()
        resp.json.return_value = {"jobs": jobs_data}
        resp.raise_for_status = MagicMock()
        return resp

    def test_returns_standard_dicts(self):
        from app.services.sources.greenhouse import fetch
        raw = [{
            "id": 4001,
            "title": "Software Engineer",
            "location": {"name": "San Francisco, CA"},
            "absolute_url": "https://boards.greenhouse.io/stripe/jobs/4001",
            "content": "Build APIs at Stripe.",
        }]
        with patch("httpx.get", return_value=self._mock_response(raw)):
            results = fetch(company_slugs=["stripe"])
        assert len(results) == 1
        job = results[0]
        assert job["source"] == "greenhouse"
        assert job["source_job_id"] == "4001"
        assert job["company"] == "stripe"
        assert job["url"] == "https://boards.greenhouse.io/stripe/jobs/4001"

    def test_multiple_slugs_merged(self):
        from app.services.sources.greenhouse import fetch
        raw_stripe = [{"id": 1, "title": "SWE", "location": {"name": "NYC"},
                       "absolute_url": "https://greenhouse.io/stripe/1", "content": "desc"}]
        raw_airbnb = [{"id": 2, "title": "SRE", "location": {"name": "SF"},
                       "absolute_url": "https://greenhouse.io/airbnb/2", "content": "desc"}]
        with patch("httpx.get", side_effect=[
            self._mock_response(raw_stripe),
            self._mock_response(raw_airbnb),
        ]):
            results = fetch(company_slugs=["stripe", "airbnb"])
        assert len(results) == 2

    def test_failed_slug_skipped(self):
        from app.services.sources.greenhouse import fetch
        import httpx
        raw_ok = [{"id": 1, "title": "SWE", "location": {"name": "NYC"},
                   "absolute_url": "https://greenhouse.io/good/1", "content": "desc"}]
        with patch("httpx.get", side_effect=[
            httpx.HTTPError("404"),
            self._mock_response(raw_ok),
        ]):
            results = fetch(company_slugs=["bad_slug", "good"])
        assert len(results) == 1


# ---------------------------------------------------------------------------
# Lever adapter
# ---------------------------------------------------------------------------

class TestLeverAdapter:
    def test_returns_standard_dicts(self):
        from app.services.sources.lever import fetch
        raw = [{
            "id": "lever-uuid-001",
            "text": "ML Engineer",
            "categories": {"location": "Remote", "team": "AI"},
            "hostedUrl": "https://jobs.lever.co/openai/lever-uuid-001",
            "descriptionPlain": "Build ML systems.",
        }]
        with patch("httpx.get", return_value=MagicMock(
            json=lambda: raw, raise_for_status=MagicMock()
        )):
            results = fetch(company_slugs=["openai"])
        assert len(results) == 1
        job = results[0]
        assert job["source"] == "lever"
        assert job["source_job_id"] == "lever-uuid-001"
        assert job["title"] == "ML Engineer"
        assert job["is_remote"] is True

    def test_failed_slug_skipped(self):
        from app.services.sources.lever import fetch
        import httpx
        with patch("httpx.get", side_effect=httpx.HTTPError("404")):
            results = fetch(company_slugs=["nonexistent"])
        assert results == []


# ---------------------------------------------------------------------------
# Ashby adapter
# ---------------------------------------------------------------------------

class TestAshbyAdapter:
    # Uses Ashby's public posting API (api.ashbyhq.com/posting-api/job-board/{slug});
    # the previous internal endpoint began 404ing for every organization.
    def test_returns_standard_dicts(self):
        from app.services.sources.ashby import fetch
        raw = {"jobs": [{
            "id": "ashby-001",
            "title": "Staff Engineer",
            "location": "New York, NY",
            "isRemote": False,
            "isListed": True,
            "jobUrl": "https://jobs.ashbyhq.com/rippling/ashby-001",
            "descriptionPlain": "Scale infrastructure.",
            "publishedAt": None,
        }]}
        with patch("httpx.get", return_value=MagicMock(
            json=lambda: raw, raise_for_status=MagicMock()
        )) as mock_get:
            results = fetch(company_slugs=["rippling"])
        assert "api.ashbyhq.com/posting-api/job-board/rippling" in mock_get.call_args[0][0]
        assert len(results) == 1
        job = results[0]
        assert job["source"] == "ashby"
        assert job["source_job_id"] == "ashby-001"
        assert job["experience_level"] == "senior"
        assert job["description"] == "Scale infrastructure."

    def test_skips_unlisted_jobs(self):
        from app.services.sources.ashby import fetch
        raw = {"jobs": [{"id": "x", "title": "SWE", "isListed": False,
                         "location": "", "descriptionPlain": ""}]}
        with patch("httpx.get", return_value=MagicMock(
            json=lambda: raw, raise_for_status=MagicMock()
        )):
            assert fetch(company_slugs=["co"]) == []

    def test_failed_slug_skipped(self):
        from app.services.sources.ashby import fetch
        import httpx
        with patch("httpx.get", side_effect=httpx.HTTPError("500")):
            results = fetch(company_slugs=["bad"])
        assert results == []


# ---------------------------------------------------------------------------
# LinkedIn guest API (httpx, mocked)
# ---------------------------------------------------------------------------

def _li_card(job_id: str, title="Software Engineer", company="Stripe",
             location="New York, NY", posted="2026-08-01") -> str:
    return f"""
    <li>
      <a href="https://www.linkedin.com/jobs/view/{title.lower().replace(' ', '-')}-at-{company.lower()}-{job_id}?refId=abc">link</a>
      <h3 class="base-search-card__title">{title}</h3>
      <h4 class="base-search-card__subtitle"><a>{company}</a></h4>
      <span class="job-search-card__location">{location}</span>
      <time class="job-search-card__listdate" datetime="{posted}">1 day ago</time>
    </li>
    """


class TestLinkedInScraper:
    _SEARCH_HTML = _li_card("4012345678")
    _POSTING_HTML = (
        '<div class="show-more-less-html__markup">'
        "Build <b>APIs</b> with Python.<br>Docker required.</div>"
    )

    @pytest.fixture(autouse=True)
    def _clear_description_cache(self):
        # Descriptions are cached for the life of the worker process, so tests
        # sharing a job id would otherwise leak results into each other.
        from app.services.sources import linkedin
        linkedin._DESC_CACHE.clear()
        yield
        linkedin._DESC_CACHE.clear()

    def _resp(self, text: str, status: int = 200) -> MagicMock:
        resp = MagicMock()
        resp.text = text
        resp.status_code = status
        resp.raise_for_status = MagicMock()
        return resp

    def _router(self, search: str, posting=None):
        """Answer by URL — detail fetches run concurrently, so order isn't fixed."""
        def _get(url, **kwargs):
            if "jobPosting" in url:
                if posting is None:
                    import httpx
                    raise httpx.HTTPError("blocked")
                if isinstance(posting, int):
                    return self._resp("", status=posting)
                return self._resp(posting)
            return self._resp(search)
        return _get

    def test_returns_standard_dicts_with_full_description(self):
        from app.services.sources.linkedin import fetch
        with patch("httpx.get", side_effect=self._router(self._SEARCH_HTML, self._POSTING_HTML)):
            results = fetch(session_cookie="", query="Software Engineer", location="New York")
        assert len(results) == 1
        job = results[0]
        assert job["source"] == "linkedin"
        assert job["title"] == "Software Engineer"
        assert job["company"] == "Stripe"
        assert job["source_job_id"] == "4012345678"
        assert job["posted_at"] == "2026-08-01"
        assert "Docker required." in job["description"]
        assert "<b>" not in job["description"]

    def test_detail_fetch_error_keeps_job_without_description(self):
        from app.services.sources.linkedin import fetch
        with patch("httpx.get", side_effect=self._router(self._SEARCH_HTML, None)):
            results = fetch(session_cookie="", query="SWE", location="NYC")
        assert len(results) == 1
        assert results[0]["description"] == ""

    def test_search_error_returns_empty(self):
        import httpx
        from app.services.sources.linkedin import fetch
        with patch("httpx.get", side_effect=httpx.HTTPError("timeout")):
            results = fetch(session_cookie="", query="SWE", location="NYC")
        assert results == []

    def test_job_id_extraction(self):
        from app.services.sources.linkedin import _job_id_from_url
        assert _job_id_from_url("https://www.linkedin.com/jobs/view/swe-at-acme-4012345678") == "4012345678"
        assert _job_id_from_url("https://www.linkedin.com/jobs/view/no-id-here") is None

    def test_cards_parsed_independently(self):
        """A card missing a field must not shift the next card's company/location."""
        from app.services.sources.linkedin import _split_cards, _parse_card
        html = (
            '<li><a href="https://www.linkedin.com/jobs/view/a-11111111">x</a>'
            '<h3 class="base-search-card__title">Backend Engineer</h3></li>'
            + _li_card("22222222", title="Data Engineer", company="Figma",
                       location="Remote")
        )
        cards = [_parse_card(c) for c in _split_cards(html)]
        assert cards[0]["title"] == "Backend Engineer"
        assert cards[0]["company"] == ""       # genuinely absent, not borrowed
        assert cards[1]["company"] == "Figma"
        assert cards[1]["location"] == "Remote"
        assert cards[1]["is_remote"] is True

    def test_paginates_until_short_page(self):
        from app.services.sources.linkedin import fetch_all
        full_page = "".join(_li_card(f"4000000{i:03d}") for i in range(10))
        pages = [full_page, _li_card("4000000999")]
        calls = []

        def _get(url, **kwargs):
            if "jobPosting" in url:
                return self._resp(self._POSTING_HTML)
            calls.append(url)
            return self._resp(pages[len(calls) - 1] if len(calls) <= len(pages) else "")

        with patch("httpx.get", side_effect=_get):
            results = fetch_all("", ["SWE"], ["NYC"], max_pages=5, max_details=0)
        # Page 1 was full so it asked for page 2; page 2 was short so it stopped.
        assert len(calls) == 2
        assert "start=0" in calls[0] and "start=10" in calls[1]
        assert len(results) == 11

    def test_deduplicates_the_same_posting_across_searches(self):
        from app.services.sources.linkedin import fetch_all
        with patch("httpx.get", side_effect=self._router(self._SEARCH_HTML, self._POSTING_HTML)):
            results = fetch_all("", ["SWE", "Backend Engineer"], ["NYC", "Boston"],
                                max_pages=1)
        assert len(results) == 1

    def test_description_budget_is_respected(self):
        from app.services.sources.linkedin import fetch_all
        search = "".join(_li_card(f"4000001{i:03d}") for i in range(3))
        detail_calls = []

        def _get(url, **kwargs):
            if "jobPosting" in url:
                detail_calls.append(url)
                return self._resp(self._POSTING_HTML)
            return self._resp(search)

        with patch("httpx.get", side_effect=_get):
            results = fetch_all("", ["SWE"], ["NYC"], max_pages=1, max_details=2)
        assert len(detail_calls) == 2
        assert sum(1 for job in results if job["description"]) == 2

    def test_gives_up_after_repeated_throttling(self):
        from app.services.sources import linkedin
        calls = []

        def _get(url, **kwargs):
            calls.append(url)
            return self._resp("", status=429)

        with patch("httpx.get", side_effect=_get), \
             patch.object(linkedin.time, "sleep"):
            results = linkedin.fetch_all("", ["a", "b", "c", "d", "e"], ["NYC"],
                                         max_pages=5)
        assert results == []
        assert len(calls) == linkedin._MAX_CONSECUTIVE_THROTTLES

    def test_the_title_gate_runs_before_the_detail_fetches(self):
        """
        The regression that left 8,800 LinkedIn jobs with no description: the
        detail budget was spent in card order, so most of it went to jobs the
        title gate rejected moments later. The gate runs first now.
        """
        from app.services.sources.linkedin import fetch_all
        search = (
            _li_card("4000002001", title="Dental Hygienist")
            + _li_card("4000002002", title="Backend Engineer")
            + _li_card("4000002003", title="Marketing Manager")
        )
        detail_calls = []

        def _get(url, **kwargs):
            if "jobPosting" in url:
                detail_calls.append(url)
                return self._resp(self._POSTING_HTML)
            return self._resp(search)

        with patch("httpx.get", side_effect=_get), \
             patch.object(linkedin_module.time, "sleep"):
            results = fetch_all("", ["Backend Engineer"], ["NYC"], max_pages=1)

        # One request, for the one job worth having a description.
        assert len(detail_calls) == 1
        assert "4000002002" in detail_calls[0]
        described = [j for j in results if j["description"]]
        assert [j["title"] for j in described] == ["Backend Engineer"]
        # The rejects are still stored — the matcher explains why it dropped
        # them, which is more useful than their silent absence.
        assert len(results) == 3

    def test_every_surviving_job_gets_a_description(self):
        from app.services.sources.linkedin import fetch_all
        search = "".join(
            _li_card(f"4000003{i:03d}", title="Backend Engineer") for i in range(6)
        )
        detail_calls = []

        def _get(url, **kwargs):
            if "jobPosting" in url:
                detail_calls.append(url)
                return self._resp(self._POSTING_HTML)
            return self._resp(search)

        with patch("httpx.get", side_effect=_get), \
             patch.object(linkedin_module.time, "sleep"):
            results = fetch_all("", ["Backend Engineer"], ["NYC"], max_pages=1)

        assert len(detail_calls) == 6
        assert all(job["description"] for job in results)

    def test_a_gate_that_rejects_everything_is_treated_as_misconfiguration(self):
        """
        If nothing passes, the gate disagrees with the very queries that found
        these jobs — that is a configuration problem, not a verdict, and
        fetching nothing would make the source produce nothing usable.
        """
        from app.services.sources.linkedin import fetch_all
        search = _li_card("4000004001", title="Backend Engineer")
        detail_calls = []

        def _get(url, **kwargs):
            if "jobPosting" in url:
                detail_calls.append(url)
                return self._resp(self._POSTING_HTML)
            return self._resp(search)

        with patch("httpx.get", side_effect=_get), \
             patch.object(linkedin_module.time, "sleep"):
            fetch_all("", ["Underwater Basket Weaving"], ["NYC"], max_pages=1)

        assert len(detail_calls) == 1

    def test_the_description_keeps_its_list_structure(self):
        # Cleaned by services.descriptions rather than by a rougher strip here,
        # so bullets survive into the text the matcher reads.
        from app.services.sources.linkedin import fetch_all
        posting = (
            '<div class="show-more-less-html__markup">'
            "<p>Build APIs.</p><ul><li>Python</li><li>Docker</li></ul></div>"
        )

        def _get(url, **kwargs):
            if "jobPosting" in url:
                return self._resp(posting)
            return self._resp(self._SEARCH_HTML)

        with patch("httpx.get", side_effect=_get), \
             patch.object(linkedin_module.time, "sleep"):
            results = fetch_all("", ["Software Engineer"], ["NYC"], max_pages=1)

        assert results[0]["description"] == "Build APIs.\n\n- Python\n- Docker"

    def test_a_200_with_unrecognisable_markup_is_reported_not_silent(self, caplog):
        """
        Markup drift is the failure mode that looks exactly like "no jobs".
        A substantial response we can't parse has to say so.
        """
        import logging
        from app.services.sources.linkedin import fetch_all
        redesigned = "<div class='totally-new-markup'>" + ("x" * 2000) + "</div>"

        with caplog.at_level(logging.ERROR, logger="app.services.sources.linkedin"):
            with patch("httpx.get", side_effect=lambda url, **kw: self._resp(redesigned)):
                results = fetch_all("", ["SWE"], ["NYC"], max_pages=2)

        assert results == []
        assert "no job cards parsed" in caplog.text

    def test_a_genuinely_empty_response_is_not_reported_as_broken(self, caplog):
        import logging
        from app.services.sources.linkedin import fetch_all

        with caplog.at_level(logging.ERROR, logger="app.services.sources.linkedin"):
            with patch("httpx.get", side_effect=lambda url, **kw: self._resp("")):
                results = fetch_all("", ["SWE"], ["NYC"], max_pages=2)

        assert results == []
        assert caplog.text == ""

    def test_recency_and_sort_params_are_sent(self):
        from app.services.sources.linkedin import fetch_all
        calls = []

        def _get(url, **kwargs):
            calls.append(url)
            return self._resp("")

        with patch("httpx.get", side_effect=_get):
            fetch_all("", ["SWE"], ["NYC"], max_pages=1, recency_hours=24)
        assert "sortBy=DD" in calls[0]
        assert "f_TPR=r86400" in calls[0]


# ---------------------------------------------------------------------------
# Indeed scraper (playwright, mocked)
# ---------------------------------------------------------------------------

class TestIndeedScraper:
    _RSS = """<?xml version="1.0"?>
    <rss><channel>
      <item>
        <title>Backend Engineer - Meta</title>
        <link>https://www.indeed.com/viewjob?jk=abc123</link>
        <description>Python and &lt;b&gt;Docker&lt;/b&gt; required.</description>
      </item>
    </channel></rss>"""

    def _resp(self, text: str) -> MagicMock:
        resp = MagicMock()
        resp.text = text
        resp.raise_for_status = MagicMock()
        return resp

    def test_returns_standard_dicts(self):
        from app.services.sources.indeed import fetch
        with patch("httpx.get", return_value=self._resp(self._RSS)):
            results = fetch(query="Backend Engineer", location="Menlo Park, CA")
        assert len(results) == 1
        job = results[0]
        assert job["source"] == "indeed"
        assert job["title"] == "Backend Engineer"
        assert job["company"] == "Meta"
        assert job["url"] == "https://www.indeed.com/viewjob?jk=abc123"
        assert "Docker" in job["description"]

    def test_empty_on_error(self):
        import httpx
        from app.services.sources.indeed import fetch
        with patch("httpx.get", side_effect=httpx.HTTPError("Timeout")):
            results = fetch(query="SWE", location="NYC")
        assert results == []

    def test_empty_on_bad_xml(self):
        from app.services.sources.indeed import fetch
        with patch("httpx.get", return_value=self._resp("not xml at all")):
            results = fetch(query="SWE", location="NYC")
        assert results == []


# ---------------------------------------------------------------------------
# Wellfound, Dice, Handshake scrapers (playwright, mocked)
# ---------------------------------------------------------------------------

class TestWellfoundScraper:
    @pytest.fixture(autouse=True)
    def _fresh_cycle(self):
        """Role pages are cached within a cycle; each test is its own cycle."""
        from app.services.sources import wellfound
        wellfound.reset_cache()
        yield
        wellfound.reset_cache()

    def test_returns_standard_dicts(self):
        import asyncio

        async def mock_scrape(*args, **kwargs):
            return [{"source": "wellfound", "source_job_id": None,
                     "title": "SWE", "company": "Startup", "location": "Remote",
                     "is_remote": True, "url": "https://wellfound.com/job/1",
                     "description": "", "experience_level": "mid"}]

        with patch("app.services.sources.wellfound._scrape", side_effect=mock_scrape):
            from app.services.sources.wellfound import fetch
            results = asyncio.run(fetch(query="SWE", location="Remote"))
        assert results[0]["source"] == "wellfound"

    def test_empty_on_error(self):
        import asyncio

        async def raise_error(*args, **kwargs):
            raise RuntimeError("Block")

        with patch("app.services.sources.wellfound._scrape", side_effect=raise_error):
            from app.services.sources import wellfound
            results = asyncio.run(wellfound.fetch(query="SWE", location="NYC"))
        assert results == []


class TestDiceScraper:
    def test_returns_standard_dicts(self):
        import asyncio

        async def mock_scrape(*args, **kwargs):
            return [{"source": "dice", "source_job_id": None,
                     "title": "DevOps Engineer", "company": "TechCo",
                     "location": "Austin, TX", "is_remote": False,
                     "url": "https://dice.com/job/1", "description": "",
                     "experience_level": "mid"}]

        with patch("app.services.sources.dice._scrape", side_effect=mock_scrape):
            from app.services.sources.dice import fetch
            results = asyncio.run(fetch(query="DevOps", location="Austin"))
        assert results[0]["source"] == "dice"

    def test_empty_on_error(self):
        import asyncio

        async def raise_error(*args, **kwargs):
            raise RuntimeError("Timeout")

        with patch("app.services.sources.dice._scrape", side_effect=raise_error):
            from app.services.sources import dice
            results = asyncio.run(dice.fetch(query="SWE", location="NYC"))
        assert results == []


class TestHandshakeScraper:
    def test_returns_standard_dicts(self):
        import asyncio

        async def mock_scrape(*args, **kwargs):
            return [{"source": "handshake", "source_job_id": None,
                     "title": "New Grad SWE", "company": "Amazon",
                     "location": "Seattle, WA", "is_remote": False,
                     "url": "https://joinhandshake.com/posting/1", "description": "",
                     "experience_level": "entry"}]

        with patch("app.services.sources.handshake._scrape", side_effect=mock_scrape):
            from app.services.sources.handshake import fetch
            results = asyncio.run(fetch(session_cookie="sess", query="SWE", location=""))
        assert results[0]["source"] == "handshake"

    def test_empty_on_error(self):
        import asyncio

        async def raise_error(*args, **kwargs):
            raise RuntimeError("Login required")

        with patch("app.services.sources.handshake._scrape", side_effect=raise_error):
            from app.services.sources import handshake
            results = asyncio.run(handshake.fetch(session_cookie="s", query="SWE", location=""))
        assert results == []


# ---------------------------------------------------------------------------
# The Muse adapter
# ---------------------------------------------------------------------------

class TestTheMuseAdapter:
    def _mock_response(self, results: list[dict], page_count: int = 1) -> MagicMock:
        resp = MagicMock()
        resp.json.return_value = {"results": results, "page_count": page_count}
        resp.raise_for_status = MagicMock()
        return resp

    def test_returns_standard_dicts(self):
        from app.services.sources.themuse import fetch
        raw = [{
            "id": 111,
            "name": "Software Engineer, Backend",
            "company": {"name": "Spotify"},
            "locations": [{"name": "New York, NY"}],
            "levels": [{"name": "Entry Level", "short_name": "entry"}],
            "refs": {"landing_page": "https://themuse.com/jobs/111"},
            "contents": "Build music systems.",
            "publication_date": "2026-06-20T00:00:00Z",
        }]
        with patch("httpx.get", return_value=self._mock_response(raw)):
            results = fetch(query="Software Engineer")
        assert len(results) >= 1
        job = results[0]
        assert job["source"] == "themuse"
        assert job["source_job_id"] == "111"
        assert job["company"] == "Spotify"
        assert job["url"] == "https://themuse.com/jobs/111"
        assert job["experience_level"] == "entry"

    def test_filters_by_query_words(self):
        from app.services.sources.themuse import fetch
        raw = [{
            "id": 112,
            "name": "Account Executive",
            "company": {"name": "Co"},
            "locations": [],
            "levels": [],
            "refs": {"landing_page": "https://themuse.com/jobs/112"},
            "contents": "Sell things.",
        }]
        with patch("httpx.get", return_value=self._mock_response(raw)):
            results = fetch(query="Software Engineer")
        assert results == []

    def test_remote_detection_from_flexible_location(self):
        from app.services.sources.themuse import fetch
        raw = [{
            "id": 113,
            "name": "Backend Engineer",
            "company": {"name": "Co"},
            "locations": [{"name": "Flexible / Remote"}],
            "levels": [],
            "refs": {"landing_page": "https://themuse.com/jobs/113"},
            "contents": "",
        }]
        with patch("httpx.get", return_value=self._mock_response(raw)):
            results = fetch(query="Engineer")
        assert results[0]["is_remote"] is True

    def test_http_error_returns_empty(self):
        from app.services.sources.themuse import fetch
        import httpx
        with patch("httpx.get", side_effect=httpx.HTTPError("timeout")):
            results = fetch(query="Engineer")
        assert results == []


# ---------------------------------------------------------------------------
# Himalayas adapter
# ---------------------------------------------------------------------------

class TestHimalayasAdapter:
    def _mock_response(self, jobs: list[dict]) -> MagicMock:
        resp = MagicMock()
        resp.json.return_value = {"jobs": jobs}
        resp.raise_for_status = MagicMock()
        return resp

    def test_returns_standard_dicts(self):
        from app.services.sources.himalayas import fetch
        raw = [{
            "title": "Senior Backend Engineer",
            "companyName": "Doist",
            "categories": ["Software Engineering"],
            "locationRestrictions": ["USA", "Canada"],
            "applicationLink": "https://himalayas.app/jobs/1/apply",
            "guid": "https://himalayas.app/jobs/1",
            "description": "Build APIs.",
            "pubDate": 1750000000,
        }]
        with patch("httpx.get", return_value=self._mock_response(raw)):
            results = fetch(query="Backend Engineer")
        assert len(results) == 1
        job = results[0]
        assert job["source"] == "himalayas"
        assert job["company"] == "Doist"
        assert job["is_remote"] is True
        assert job["location"] == "USA, Canada"
        assert job["experience_level"] == "senior"

    def test_filters_by_query(self):
        from app.services.sources.himalayas import fetch
        raw = [{
            "title": "Marketing Manager",
            "companyName": "Co",
            "categories": ["Marketing"],
            "guid": "https://himalayas.app/jobs/2",
            "description": "",
        }]
        with patch("httpx.get", return_value=self._mock_response(raw)):
            results = fetch(query="Software Engineer")
        assert results == []

    def test_http_error_returns_empty(self):
        from app.services.sources.himalayas import fetch
        import httpx
        with patch("httpx.get", side_effect=httpx.HTTPError("timeout")):
            results = fetch(query="Engineer")
        assert results == []

    def test_a_nested_company_object_yields_its_name(self):
        """
        Every stored Himalayas job had the literal string "name" as its
        employer: the key got read where the value was meant. The company
        arrives as a string on some records and an object on others, so both
        shapes are read.
        """
        from app.services.sources.himalayas import fetch
        raw = [{
            "title": "Backend Engineer",
            "company": {"name": "Doist", "logo": "https://x/y.png"},
            "categories": ["Software Engineering"],
            "guid": "https://himalayas.app/jobs/9",
            "description": "Build APIs.",
        }]
        with patch("httpx.get", return_value=self._mock_response(raw)):
            results = fetch(query="Backend Engineer")
        assert results[0]["company"] == "Doist"

    def test_a_bare_key_name_is_never_stored_as_a_company(self):
        from app.services.sources.himalayas import fetch
        raw = [{
            "title": "Backend Engineer",
            "companyName": "name",
            "categories": ["Software Engineering"],
            "guid": "https://himalayas.app/jobs/10",
        }]
        with patch("httpx.get", return_value=self._mock_response(raw)):
            results = fetch(query="Backend Engineer")
        assert results[0]["company"] == ""

    def test_object_shaped_location_restrictions_do_not_leak_keys(self):
        from app.services.sources.himalayas import fetch
        raw = [{
            "title": "Backend Engineer",
            "companyName": "Doist",
            "categories": ["Software Engineering"],
            "guid": "https://himalayas.app/jobs/11",
            "locationRestrictions": [{"name": "USA"}, {"name": "Canada"}],
        }]
        with patch("httpx.get", return_value=self._mock_response(raw)):
            results = fetch(query="Backend Engineer")
        assert results[0]["location"] == "USA, Canada"


# ---------------------------------------------------------------------------
# RemoteOK adapter
# ---------------------------------------------------------------------------

class TestRemoteOKAdapter:
    def _mock_response(self, items: list) -> MagicMock:
        resp = MagicMock()
        resp.json.return_value = items
        resp.raise_for_status = MagicMock()
        return resp

    def test_returns_standard_dicts(self):
        from app.services.sources.remoteok import fetch
        raw = [{
            "id": "77",
            "position": "Backend Engineer",
            "company": "Remote Co",
            "location": "Worldwide",
            "url": "https://remoteok.com/l/77",
            "description": "<p>Python and Go.</p>",
            "tags": ["python", "go"],
            "date": "2026-08-01T00:00:00+00:00",
        }]
        with patch("httpx.get", return_value=self._mock_response(raw)):
            results = fetch(query="Backend Engineer")
        assert len(results) == 1
        assert results[0]["company"] == "Remote Co"
        # Cleaned on the way out of the adapter, like every other write path.
        assert results[0]["description"] == "Python and Go."

    def test_a_challenge_page_description_drops_the_listing(self):
        """
        RemoteOK sits behind a bot wall that sometimes answers with its
        challenge page. Storing that is worse than storing nothing — it reads
        as a real description to the filter, the matcher and the generator.
        """
        from app.services.sources.remoteok import fetch
        raw = [{
            "id": "78",
            "position": "Backend Engineer",
            "company": "Remote Co",
            "url": "https://remoteok.com/l/78",
            "description": "Verify you are human. Performance & security by Cloudflare",
            "tags": ["python"],
        }]
        with patch("httpx.get", return_value=self._mock_response(raw)):
            results = fetch(query="Backend Engineer")
        assert results == []

    def test_a_listing_with_no_description_is_still_kept(self):
        # Nothing to enrich from is different from a wall; enrichment can go
        # and fetch this one's real text later.
        from app.services.sources.remoteok import fetch
        raw = [{
            "id": "79",
            "position": "Backend Engineer",
            "company": "Remote Co",
            "url": "https://remoteok.com/l/79",
            "description": "",
            "tags": ["python"],
        }]
        with patch("httpx.get", return_value=self._mock_response(raw)):
            results = fetch(query="Backend Engineer")
        assert len(results) == 1


# ---------------------------------------------------------------------------
# Jobicy adapter
# ---------------------------------------------------------------------------

class TestJobicyAdapter:
    def _mock_response(self, jobs: list[dict]) -> MagicMock:
        resp = MagicMock()
        resp.json.return_value = {"jobs": jobs}
        resp.raise_for_status = MagicMock()
        return resp

    def test_returns_standard_dicts(self):
        from app.services.sources.jobicy import fetch
        raw = [{
            "id": 555,
            "url": "https://jobicy.com/jobs/555",
            "jobTitle": "Full Stack Developer",
            "companyName": "Remote Co",
            "jobGeo": "USA",
            "jobLevel": "Any",
            "jobExcerpt": "Build web apps.",
            "jobDescription": "Build web apps with React and Node.",
            "pubDate": "2026-06-25 10:00:00",
        }]
        with patch("httpx.get", return_value=self._mock_response(raw)):
            results = fetch(query="Full Stack Developer")
        assert len(results) == 1
        job = results[0]
        assert job["source"] == "jobicy"
        assert job["source_job_id"] == "555"
        assert job["company"] == "Remote Co"
        assert job["is_remote"] is True
        assert job["location"] == "USA"

    def test_senior_level_from_job_level_field(self):
        from app.services.sources.jobicy import fetch
        raw = [{
            "id": 556,
            "url": "https://jobicy.com/jobs/556",
            "jobTitle": "Backend Developer",
            "companyName": "Co",
            "jobGeo": "Anywhere",
            "jobLevel": "Senior",
            "jobDescription": "APIs.",
        }]
        with patch("httpx.get", return_value=self._mock_response(raw)):
            results = fetch(query="Backend")
        assert results[0]["experience_level"] == "senior"

    def test_http_error_returns_empty(self):
        from app.services.sources.jobicy import fetch
        import httpx
        with patch("httpx.get", side_effect=httpx.HTTPError("timeout")):
            results = fetch(query="Engineer")
        assert results == []


# ---------------------------------------------------------------------------
# Hacker News "Who is hiring?" adapter
# ---------------------------------------------------------------------------

class TestHNHiringAdapter:
    def _search_resp(self):
        resp = MagicMock()
        resp.json.return_value = {"hits": [
            {"objectID": "40001", "title": "Ask HN: Who is hiring? (July 2026)"},
        ]}
        resp.raise_for_status = MagicMock()
        return resp

    def _item_resp(self, children):
        resp = MagicMock()
        resp.json.return_value = {"id": 40001, "children": children}
        resp.raise_for_status = MagicMock()
        return resp

    def test_parses_top_level_comments_as_jobs(self):
        from app.services.sources.hnhiring import fetch
        children = [
            {
                "id": 40002,
                "created_at": "2026-07-01T12:00:00Z",
                "text": "<p>Acme Robotics | Software Engineer | Remote (US) | $150k</p>"
                        "<p>We build robots. Python and Go stack.</p>",
            },
            {"id": 40003, "created_at": "2026-07-01T13:00:00Z", "text": None},  # dead
        ]
        with patch("httpx.get", side_effect=[self._search_resp(), self._item_resp(children)]):
            results = fetch(queries=["Software Engineer"])
        assert len(results) == 1
        job = results[0]
        assert job["source"] == "hnhiring"
        assert job["company"] == "Acme Robotics"
        assert job["title"] == "Software Engineer"
        assert job["is_remote"] is True
        assert job["url"] == "https://news.ycombinator.com/item?id=40002"
        assert "Python and Go stack." in job["description"]

    def test_filters_comments_not_matching_queries(self):
        from app.services.sources.hnhiring import fetch
        children = [
            {"id": 1, "created_at": "", "text": "<p>Co | Accountant | NYC</p><p>Finance only.</p>"},
        ]
        with patch("httpx.get", side_effect=[self._search_resp(), self._item_resp(children)]):
            results = fetch(queries=["Kubernetes Wizard"])
        assert results == []

    def test_unpiped_header_falls_back_to_first_line(self):
        from app.services.sources.hnhiring import fetch
        children = [
            {"id": 2, "created_at": "", "text": "<p>Hiring a backend engineer at Initech, onsite Austin.</p>"},
        ]
        with patch("httpx.get", side_effect=[self._search_resp(), self._item_resp(children)]):
            results = fetch(queries=["Backend Engineer"])
        assert len(results) == 1
        assert "backend engineer" in results[0]["title"].lower()

    def test_http_error_returns_empty(self):
        from app.services.sources.hnhiring import fetch
        import httpx
        with patch("httpx.get", side_effect=httpx.HTTPError("down")):
            results = fetch(queries=["Engineer"])
        assert results == []

    def test_no_thread_found_returns_empty(self):
        from app.services.sources.hnhiring import fetch
        resp = MagicMock()
        resp.json.return_value = {"hits": []}
        resp.raise_for_status = MagicMock()
        with patch("httpx.get", return_value=resp):
            results = fetch(queries=["Engineer"])
        assert results == []

    def test_location_first_header_still_finds_role_title(self):
        from app.services.sources.hnhiring import _parse_header
        company, title = _parse_header(
            "Blaine, WA | CaseLight Systems Inc. | Remote (US Only) | Founding Systems Engineer | $150k"
        )
        assert title == "Founding Systems Engineer"
        assert company == "CaseLight Systems Inc."

    def test_a_location_is_never_stored_as_the_company(self):
        """
        The thread's template is `Company | Role | Location | ...`, so the
        company is the earliest segment that is neither. Reading it as "the
        first segment without a comma" instead is what filed
        "New York, NY (In-Office)" as an employer.
        """
        from app.services.sources.hnhiring import _parse_header
        company, title = _parse_header(
            "Acme Corp | Senior Backend Engineer | New York, NY (In-Office) | Full-time"
        )
        assert company == "Acme Corp"
        assert title == "Senior Backend Engineer"

    def test_a_company_with_a_comma_beats_the_location_beside_it(self):
        from app.services.sources.hnhiring import _parse_header
        company, _ = _parse_header("ACME Corp, Inc | Senior Engineer | NYC")
        assert company == "ACME Corp, Inc"

    def test_a_company_named_after_a_region_is_not_mistaken_for_one(self):
        # "UK Power Networks" is an employer. Rejecting it to avoid a location
        # would trade a wrong answer for a missing job, which is worse.
        from app.services.sources.hnhiring import _parse_header
        company, _ = _parse_header("UK Power Networks | Systems Engineer | London")
        assert company == "UK Power Networks"

    def test_a_header_naming_no_company_is_dropped(self):
        """
        The company is half the dedupe key; an empty one collapses unrelated
        posts into each other, so a post that names only a place and a role is
        not stored at all.
        """
        from app.services.sources.hnhiring import fetch
        children = [
            {"id": 3, "created_at": "",
             "text": "<p>New York, NY (In-Office) | Backend Engineer</p>"
                     "<p>We use Python.</p>"},
        ]
        with patch("httpx.get", side_effect=[self._search_resp(), self._item_resp(children)]):
            results = fetch(queries=["Backend Engineer"])
        assert results == []


# ---------------------------------------------------------------------------
# Workable adapter
# ---------------------------------------------------------------------------

class TestWorkableAdapter:
    def _resp(self, payload):
        resp = MagicMock()
        resp.json.return_value = payload
        resp.raise_for_status = MagicMock()
        return resp

    def test_returns_standard_dicts(self):
        from app.services.sources.workable import fetch
        payload = {
            "name": "Acme Co",
            "jobs": [{
                "title": "Backend Engineer",
                "shortcode": "AB12CD",
                "url": "https://apply.workable.com/acme-co/j/AB12CD/",
                "description": "<p>Build APIs with Python and Docker.</p>",
                "city": "Berlin", "state": "", "country": "Germany",
                "telecommuting": False,
                "published_on": "2026-06-20",
            }],
        }
        with patch("httpx.get", return_value=self._resp(payload)):
            results = fetch(company_slugs=["acme-co"])
        assert len(results) == 1
        job = results[0]
        assert job["source"] == "workable"
        assert job["company"] == "Acme Co"
        assert job["source_job_id"] == "AB12CD"
        assert job["location"] == "Berlin, Germany"
        assert "Python" in job["description"]

    def test_telecommuting_marks_remote(self):
        from app.services.sources.workable import fetch
        payload = {"name": "Co", "jobs": [{
            "title": "SWE", "shortcode": "X", "url": "https://apply.workable.com/co/j/X/",
            "description": "", "city": "", "state": "", "country": "", "telecommuting": True,
        }]}
        with patch("httpx.get", return_value=self._resp(payload)):
            results = fetch(company_slugs=["co"])
        assert results[0]["is_remote"] is True

    def test_failed_slug_skipped(self):
        from app.services.sources.workable import fetch
        import httpx
        with patch("httpx.get", side_effect=httpx.HTTPError("404")):
            assert fetch(company_slugs=["gone"]) == []


# ---------------------------------------------------------------------------
# Recruitee adapter
# ---------------------------------------------------------------------------

class TestRecruiteeAdapter:
    def _resp(self, payload):
        resp = MagicMock()
        resp.json.return_value = payload
        resp.raise_for_status = MagicMock()
        return resp

    def test_returns_standard_dicts(self):
        from app.services.sources.recruitee import fetch
        payload = {"offers": [{
            "id": 987,
            "title": "Full Stack Developer",
            "description": "<p>React and Node.</p>",
            "location": "Amsterdam, Netherlands",
            "remote": False,
            "careers_url": "https://widgetcorp.recruitee.com/o/full-stack-developer",
            "created_at": "2026-06-25",
            "company_name": "WidgetCorp",
        }]}
        with patch("httpx.get", return_value=self._resp(payload)):
            results = fetch(company_slugs=["widgetcorp"])
        assert len(results) == 1
        job = results[0]
        assert job["source"] == "recruitee"
        assert job["source_job_id"] == "987"
        assert job["company"] == "WidgetCorp"
        assert job["url"].endswith("/o/full-stack-developer")

    def test_remote_flag(self):
        from app.services.sources.recruitee import fetch
        payload = {"offers": [{"id": 1, "title": "SWE", "description": "",
                               "location": "", "remote": True, "careers_url": "u"}]}
        with patch("httpx.get", return_value=self._resp(payload)):
            results = fetch(company_slugs=["co"])
        assert results[0]["is_remote"] is True

    def test_failed_slug_skipped(self):
        from app.services.sources.recruitee import fetch
        import httpx
        with patch("httpx.get", side_effect=httpx.HTTPError("404")):
            assert fetch(company_slugs=["gone"]) == []


# ---------------------------------------------------------------------------
# SmartRecruiters adapter
# ---------------------------------------------------------------------------

class TestSmartRecruitersAdapter:
    def _resp(self, payload):
        resp = MagicMock()
        resp.json.return_value = payload
        resp.raise_for_status = MagicMock()
        return resp

    def test_returns_standard_dicts_with_detail_description(self):
        from app.services.sources.smartrecruiters import fetch
        listing = {"content": [{
            "id": "744000012",
            "name": "Software Engineer",
            "location": {"city": "San Francisco", "region": "CA", "country": "us", "remote": False},
            "company": {"name": "Databricks"},
            "releasedDate": "2026-06-28T00:00:00Z",
        }]}
        detail = {"jobAd": {"sections": {
            "jobDescription": {"title": "Job Description", "text": "Build Spark pipelines."},
            "qualifications": {"title": "Qualifications", "text": "Python, Scala."},
        }}}
        with patch("httpx.get", side_effect=[self._resp(listing), self._resp(detail)]):
            results = fetch(company_slugs=["Databricks"])
        assert len(results) == 1
        job = results[0]
        assert job["source"] == "smartrecruiters"
        assert job["company"] == "Databricks"
        assert job["url"] == "https://jobs.smartrecruiters.com/Databricks/744000012"
        assert "Spark" in job["description"]
        assert "Scala" in job["description"]

    def test_detail_error_keeps_job_without_description(self):
        from app.services.sources.smartrecruiters import fetch
        import httpx
        listing = {"content": [{"id": "1", "name": "SWE", "location": {}, "company": {}}]}
        with patch("httpx.get", side_effect=[self._resp(listing), httpx.HTTPError("500")]):
            results = fetch(company_slugs=["co"])
        assert len(results) == 1
        assert results[0]["description"] == ""

    def test_failed_slug_skipped(self):
        from app.services.sources.smartrecruiters import fetch
        import httpx
        with patch("httpx.get", side_effect=httpx.HTTPError("404")):
            assert fetch(company_slugs=["gone"]) == []


# ---------------------------------------------------------------------------
# Jooble adapter
# ---------------------------------------------------------------------------

class TestJoobleAdapter:
    def _resp(self, payload):
        resp = MagicMock()
        resp.json.return_value = payload
        resp.raise_for_status = MagicMock()
        return resp

    def test_returns_standard_dicts(self):
        from app.services.sources.jooble import fetch
        payload = {"totalCount": 1, "jobs": [{
            "id": 555111,
            "title": "Software Engineer",
            "location": "Boston, MA",
            "snippet": "Java and Spring Boot experience...",
            "link": "https://jooble.org/desc/555111",
            "company": "Initech",
            "updated": "2026-06-30T00:00:00.000+0000",
        }]}
        with patch("httpx.post", return_value=self._resp(payload)) as mock_post:
            results = fetch(api_key="KEY", query="Software Engineer", location="Boston")
        assert len(results) == 1
        job = results[0]
        assert job["source"] == "jooble"
        assert job["company"] == "Initech"
        assert "KEY" in mock_post.call_args[0][0]
        assert mock_post.call_args[1]["json"] == {"keywords": "Software Engineer", "location": "Boston"}

    def test_http_error_returns_empty(self):
        from app.services.sources.jooble import fetch
        import httpx
        with patch("httpx.post", side_effect=httpx.HTTPError("down")):
            assert fetch(api_key="KEY", query="SWE", location="NYC") == []


# ---------------------------------------------------------------------------
# Findwork adapter
# ---------------------------------------------------------------------------

class TestFindworkAdapter:
    def _resp(self, payload):
        resp = MagicMock()
        resp.json.return_value = payload
        resp.raise_for_status = MagicMock()
        return resp

    def test_returns_standard_dicts(self):
        from app.services.sources.findwork import fetch
        payload = {"results": [{
            "id": 321,
            "role": "Backend Developer",
            "company_name": "Hooli",
            "location": "Remote",
            "remote": True,
            "text": "<p>Python, Django, PostgreSQL.</p>",
            "date_posted": "2026-06-29T12:00:00Z",
            "url": "https://findwork.dev/321/backend-developer",
        }]}
        with patch("httpx.get", return_value=self._resp(payload)) as mock_get:
            results = fetch(api_key="FWKEY", query="Backend Developer")
        assert len(results) == 1
        job = results[0]
        assert job["source"] == "findwork"
        assert job["company"] == "Hooli"
        assert job["is_remote"] is True
        assert mock_get.call_args[1]["headers"]["Authorization"] == "Token FWKEY"

    def test_http_error_returns_empty(self):
        from app.services.sources.findwork import fetch
        import httpx
        with patch("httpx.get", side_effect=httpx.HTTPError("down")):
            assert fetch(api_key="K", query="SWE") == []


# ---------------------------------------------------------------------------
# Workday adapter
# ---------------------------------------------------------------------------

class TestWorkdayAdapter:
    def _post_resp(self, postings):
        resp = MagicMock()
        resp.json.return_value = {"total": len(postings), "jobPostings": postings}
        resp.raise_for_status = MagicMock()
        return resp

    def _detail_resp(self, info):
        resp = MagicMock()
        resp.json.return_value = {"jobPostingInfo": info}
        resp.raise_for_status = MagicMock()
        return resp

    def test_returns_standard_dicts_with_detail(self):
        from app.services.sources.workday import fetch
        posting = {
            "title": "Software Engineer (P3)",
            "externalPath": "/job/USA-GA-Atlanta/Software-Engineer_JR-1",
            "locationsText": "USA, GA, Atlanta",
            "postedOn": "Posted Yesterday",
        }
        info = {
            "jobDescription": "<p>Build <b>backend</b> services in Java.</p>",
            "location": "USA, GA, Atlanta",
            "externalUrl": "https://workday.wd5.myworkdayjobs.com/Workday/job/x",
            "startDate": "2026-06-30",
        }
        with patch("httpx.post", return_value=self._post_resp([posting])):
            with patch("httpx.get", return_value=self._detail_resp(info)):
                results = fetch(tenant_specs=["workday:wd5:Workday"],
                                queries=["Software Engineer"])
        assert len(results) == 1
        job = results[0]
        assert job["source"] == "workday"
        assert job["company"] == "workday"
        assert job["posted_at"] == "2026-06-30"
        assert "backend" in job["description"] and "<b>" not in job["description"]
        assert job["url"].startswith("https://workday.wd5.myworkdayjobs.com/")

    def test_dedupes_across_queries(self):
        from app.services.sources.workday import fetch
        posting = {"title": "SWE", "externalPath": "/job/X/SWE_1", "postedOn": "Posted Today"}
        with patch("httpx.post", return_value=self._post_resp([posting])):
            with patch("httpx.get", return_value=self._detail_resp({})):
                results = fetch(tenant_specs=["a:wd1:Site"], queries=["SWE", "Software Engineer"])
        assert len(results) == 1

    def test_invalid_tenant_spec_skipped(self):
        from app.services.sources.workday import fetch
        assert fetch(tenant_specs=["justonepart"], queries=["SWE"]) == []

    def test_list_error_skipped(self):
        from app.services.sources.workday import fetch
        import httpx
        with patch("httpx.post", side_effect=httpx.HTTPError("blocked")):
            assert fetch(tenant_specs=["a:wd1:Site"], queries=["SWE"]) == []

    def test_relative_posted_parsing(self):
        from app.services.sources.workday import _posted_at_from_text
        from datetime import datetime, timezone
        assert _posted_at_from_text("Posted Today")[:10] == datetime.now(timezone.utc).isoformat()[:10]
        assert _posted_at_from_text("Posted 30+ Days Ago") is not None
        assert _posted_at_from_text("") is None


# ---------------------------------------------------------------------------
# Careerjet adapter
# ---------------------------------------------------------------------------

class TestCareerjetAdapter:
    def _resp(self, payload):
        resp = MagicMock()
        resp.json.return_value = payload
        resp.raise_for_status = MagicMock()
        return resp

    def test_returns_standard_dicts(self):
        from app.services.sources.careerjet import fetch
        payload = {"type": "JOBS", "hits": 1, "jobs": [{
            "title": "Software Engineer",
            "company": "Initech",
            "locations": "London",
            "url": "https://jobviewtrack.com/x/job1",
            "description": "Build things in Java and Python...",
            "date": "2026-07-01",
        }]}
        with patch("httpx.get", return_value=self._resp(payload)) as mock_get:
            results = fetch(affid="AFF123", query="Software Engineer", location="London, United Kingdom")
        assert len(results) == 1
        job = results[0]
        assert job["source"] == "careerjet"
        assert job["company"] == "Initech"
        params = mock_get.call_args[1]["params"]
        assert params["affid"] == "AFF123"
        assert params["locale_code"] == "en_GB"  # locale picked from location

    def test_us_locale_default(self):
        from app.services.sources.careerjet import fetch
        with patch("httpx.get", return_value=self._resp({"type": "JOBS", "jobs": []})) as mock_get:
            fetch(affid="A", query="SWE", location="United States")
        assert mock_get.call_args[1]["params"]["locale_code"] == "en_US"

    def test_non_jobs_payload_returns_empty(self):
        from app.services.sources.careerjet import fetch
        with patch("httpx.get", return_value=self._resp({"type": "ERROR", "error": "bad affid"})):
            assert fetch(affid="A", query="SWE", location="NYC") == []

    def test_http_error_returns_empty(self):
        from app.services.sources.careerjet import fetch
        import httpx
        with patch("httpx.get", side_effect=httpx.HTTPError("down")):
            assert fetch(affid="A", query="SWE", location="NYC") == []


def _li_card_without_date(job_id: str) -> str:
    """A search card with no <time> element — the case that skips age checks."""
    return re.sub(r"<time[^>]*>.*?</time>", "", _li_card(job_id))


def _linkedin_router(search: str, posting=None):
    """Answer by URL — detail fetches run concurrently, so order isn't fixed."""
    def _resp(text, status=200):
        resp = MagicMock()
        resp.text = text
        resp.status_code = status
        resp.raise_for_status = MagicMock()
        return resp

    def _get(url, **kwargs):
        if "jobPosting" in url:
            if posting is None:
                import httpx
                raise httpx.HTTPError("blocked")
            return _resp(posting)
        return _resp(search)
    return _get


class TestLinkedInPostingDates:
    """
    An undated job skips the fetcher's age check entirely, so a year-old
    posting that stopped taking applications lands in the list looking fresh.
    The posting page is already fetched for the description — the date is free.
    """

    @pytest.fixture(autouse=True)
    def _clear_description_cache(self):
        from app.services.sources import linkedin
        linkedin._DESC_CACHE.clear()
        yield
        linkedin._DESC_CACHE.clear()

    def test_a_card_datetime_with_a_time_component_still_parses(self):
        """Requiring the quote straight after the date dropped these silently."""
        from app.services.sources.linkedin import _parse_card
        card = _li_card("4012345678", posted="2026-08-01T09:30:00")
        assert _parse_card(card)["posted_at"] == "2026-08-01"

    def test_a_card_with_no_time_element_has_no_date(self):
        from app.services.sources.linkedin import _parse_card
        assert _parse_card(_li_card_without_date("4012345678"))["posted_at"] is None

    def test_json_ld_date_is_preferred(self):
        from app.services.sources.linkedin import _extract_posted_at
        html = '<script type="application/ld+json">{"datePosted": "2025-03-14T00:00:00.000Z"}</script>'
        assert _extract_posted_at(html) == "2025-03-14"

    def test_a_datetime_attribute_is_read(self):
        from app.services.sources.linkedin import _extract_posted_at
        assert _extract_posted_at('<time datetime="2026-07-04"></time>') == "2026-07-04"

    def test_a_relative_phrase_becomes_a_date(self):
        """
        Approximate beats absent: absent means no age check at all, which is
        exactly how a year-old posting gets through.
        """
        from datetime import date, timedelta
        from app.services.sources.linkedin import _extract_posted_at
        html = '<span class="posted-time-ago__text">1 year ago</span>'
        expected = (date.today() - timedelta(days=365)).isoformat()
        assert _extract_posted_at(html) == expected

    @pytest.mark.parametrize("phrase,days", [
        ("3 days ago", 3), ("2 weeks ago", 14), ("6 months ago", 180),
        ("1 hour ago", 0), ("45 minutes ago", 0),
    ])
    def test_the_relative_units_convert(self, phrase, days):
        from datetime import date, timedelta
        from app.services.sources.linkedin import _extract_posted_at
        expected = (date.today() - timedelta(days=days)).isoformat()
        assert _extract_posted_at(f"<span>{phrase}</span>") == expected

    def test_a_page_with_no_date_gives_none_rather_than_today(self):
        """Guessing "today" would make a stale posting look brand new."""
        from app.services.sources.linkedin import _extract_posted_at
        assert _extract_posted_at("<div>no dates here</div>") is None

    def test_the_detail_page_fills_a_date_the_card_lacked(self):
        from datetime import date, timedelta
        from app.services.sources.linkedin import fetch
        posting = ('<div class="show-more-less-html__markup">Build APIs.</div>'
                   '<span class="posted-time-ago__text">2 weeks ago</span>')
        with patch("httpx.get",
                   side_effect=_linkedin_router(_li_card_without_date("4012345678"),
                                                posting)):
            results = fetch(session_cookie="", query="SWE", location="NYC")
        assert results[0]["posted_at"] == (date.today() - timedelta(days=14)).isoformat()

    def test_the_cards_exact_date_beats_the_pages_approximation(self):
        from app.services.sources.linkedin import fetch
        posting = ('<div class="show-more-less-html__markup">Build APIs.</div>'
                   '<span class="posted-time-ago__text">2 weeks ago</span>')
        with patch("httpx.get",
                   side_effect=_linkedin_router(_li_card("4012345678",
                                                         posted="2026-08-01"), posting)):
            results = fetch(session_cookie="", query="SWE", location="NYC")
        assert results[0]["posted_at"] == "2026-08-01"

    def test_a_failed_detail_fetch_leaves_the_date_alone(self):
        from app.services.sources.linkedin import fetch
        with patch("httpx.get",
                   side_effect=_linkedin_router(_li_card("4012345678",
                                                         posted="2026-08-01"), None)):
            results = fetch(session_cookie="", query="SWE", location="NYC")
        assert results[0]["posted_at"] == "2026-08-01"

    def test_the_cache_carries_the_date_too(self):
        """A second job id hitting the cache must not lose the date."""
        from app.services.sources import linkedin
        linkedin._cache_description("42", "text", "2026-05-01")
        assert linkedin._fetch_description("42", {}) == ("text", "2026-05-01")


# ---------------------------------------------------------------------------
# A JSON null where a nested object was expected
# ---------------------------------------------------------------------------

class TestABoardThatSendsNullWhereAnObjectGoes:
    """
    `item.get("location", {})` returns the default only when the key is
    *absent*. A board that ships `"location": null` — which every one of these
    APIs does for a posting with no location on it — hands back None, and the
    `.get("name")` behind it raises AttributeError.

    The fetch loop catches that per job, so the posting is silently dropped:
    not stale, not a duplicate, just gone, on a board that is otherwise
    working. Under "find every job" that is the expensive kind of bug, because
    the numbers still look fine.
    """

    def test_greenhouse_keeps_a_posting_with_no_location_object(self):
        from app.services.sources.greenhouse import fetch

        resp = MagicMock(raise_for_status=MagicMock())
        resp.json.return_value = {"jobs": [{
            "id": 4001, "title": "Software Engineer", "location": None,
            "absolute_url": "https://boards.greenhouse.io/stripe/jobs/4001",
            "content": "Build APIs.",
        }]}
        with patch("httpx.get", return_value=resp):
            results = fetch(company_slugs=["stripe"])

        assert [job["location"] for job in results] == [""]

    def test_lever_keeps_a_posting_with_no_categories(self):
        from app.services.sources.lever import fetch

        raw = [{"id": "u1", "text": "ML Engineer", "categories": None,
                "hostedUrl": "https://jobs.lever.co/openai/u1",
                "descriptionPlain": "Build ML systems."}]
        with patch("httpx.get", return_value=MagicMock(
            json=lambda: raw, raise_for_status=MagicMock()
        )):
            results = fetch(company_slugs=["openai"])

        assert [job["location"] for job in results] == [""]

    def test_adzuna_keeps_a_posting_with_neither_company_nor_location(self):
        from app.services.sources.adzuna import fetch

        resp = MagicMock(raise_for_status=MagicMock())
        resp.json.return_value = {"results": [{
            "id": "AZ1", "title": "Python Engineer", "company": None,
            "location": None, "redirect_url": "https://adzuna.com/jobs/AZ1",
            "description": "Build things.",
        }]}
        with patch("httpx.get", return_value=resp):
            results = fetch(app_id="ID", app_key="KEY", query="Python", location="NY")

        assert [(job["company"], job["location"]) for job in results] == [("", "")]
