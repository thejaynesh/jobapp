from unittest.mock import patch, MagicMock, AsyncMock


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

class TestLinkedInScraper:
    _SEARCH_HTML = """
    <li>
      <a href="https://www.linkedin.com/jobs/view/software-engineer-at-stripe-4012345678?refId=abc">link</a>
      <h3 class="base-search-card__title">Software Engineer</h3>
      <h4 class="base-search-card__subtitle"><a>Stripe</a></h4>
      <span class="job-search-card__location">New York, NY</span>
    </li>
    """
    _POSTING_HTML = (
        '<div class="show-more-less-html__markup">'
        "Build <b>APIs</b> with Python.<br>Docker required.</div>"
    )

    def _resp(self, text: str) -> MagicMock:
        resp = MagicMock()
        resp.text = text
        resp.raise_for_status = MagicMock()
        return resp

    def test_returns_standard_dicts_with_full_description(self):
        from app.services.sources.linkedin import fetch
        with patch("httpx.get", side_effect=[self._resp(self._SEARCH_HTML), self._resp(self._POSTING_HTML)]):
            results = fetch(session_cookie="", query="Software Engineer", location="New York")
        assert len(results) == 1
        job = results[0]
        assert job["source"] == "linkedin"
        assert job["title"] == "Software Engineer"
        assert job["company"] == "Stripe"
        assert job["source_job_id"] == "4012345678"
        assert "Docker required." in job["description"]
        assert "<b>" not in job["description"]

    def test_detail_fetch_error_keeps_job_without_description(self):
        import httpx
        from app.services.sources.linkedin import fetch
        with patch("httpx.get", side_effect=[self._resp(self._SEARCH_HTML), httpx.HTTPError("blocked")]):
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
