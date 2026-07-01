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
    def test_returns_standard_dicts(self):
        from app.services.sources.ashby import fetch
        raw = {"jobPostings": [{
            "id": "ashby-001",
            "title": "Staff Engineer",
            "locationName": "New York, NY",
            "isRemote": False,
            "jobUrl": "https://jobs.ashbyhq.com/rippling/ashby-001",
            "descriptionHtml": "<p>Scale infrastructure.</p>",
        }]}
        with patch("httpx.get", return_value=MagicMock(
            json=lambda: raw, raise_for_status=MagicMock()
        )):
            results = fetch(company_slugs=["rippling"])
        assert len(results) == 1
        job = results[0]
        assert job["source"] == "ashby"
        assert job["source_job_id"] == "ashby-001"
        assert job["experience_level"] == "senior"

    def test_failed_slug_skipped(self):
        from app.services.sources.ashby import fetch
        import httpx
        with patch("httpx.get", side_effect=httpx.HTTPError("500")):
            results = fetch(company_slugs=["bad"])
        assert results == []


# ---------------------------------------------------------------------------
# LinkedIn scraper (playwright, mocked)
# ---------------------------------------------------------------------------

class TestLinkedInScraper:
    def test_returns_standard_dicts(self):
        import asyncio

        async def mock_scrape(*args, **kwargs):
            return [{
                "source": "linkedin",
                "source_job_id": None,
                "title": "Software Engineer",
                "company": "Stripe",
                "location": "New York, NY",
                "is_remote": False,
                "url": "https://linkedin.com/jobs/1",
                "description": "",
                "experience_level": "mid",
            }]

        with patch("app.services.sources.linkedin._scrape", side_effect=mock_scrape):
            from app.services.sources.linkedin import fetch
            results = asyncio.run(fetch(
                session_cookie="test_cookie",
                query="Software Engineer",
                location="New York",
            ))

        assert len(results) == 1
        assert results[0]["source"] == "linkedin"
        assert results[0]["company"] == "Stripe"

    def test_empty_on_playwright_error(self):
        import asyncio

        async def raise_error(*args, **kwargs):
            raise RuntimeError("Browser crash")

        with patch("app.services.sources.linkedin._scrape", side_effect=raise_error):
            from app.services.sources import linkedin
            results = asyncio.run(linkedin.fetch(
                session_cookie="cookie",
                query="SWE",
                location="NYC",
            ))
        assert results == []


# ---------------------------------------------------------------------------
# Indeed scraper (playwright, mocked)
# ---------------------------------------------------------------------------

class TestIndeedScraper:
    def test_returns_standard_dicts(self):
        import asyncio

        async def mock_scrape(*args, **kwargs):
            return [{
                "source": "indeed",
                "source_job_id": None,
                "title": "Backend Engineer",
                "company": "Meta",
                "location": "Menlo Park, CA",
                "is_remote": False,
                "url": "https://indeed.com/viewjob?jk=abc123",
                "description": "",
                "experience_level": "mid",
            }]

        with patch("app.services.sources.indeed._scrape", side_effect=mock_scrape):
            from app.services.sources.indeed import fetch
            results = asyncio.run(fetch(query="Backend Engineer", location="Menlo Park"))

        assert len(results) == 1
        assert results[0]["source"] == "indeed"

    def test_empty_on_error(self):
        import asyncio

        async def raise_error(*args, **kwargs):
            raise RuntimeError("Timeout")

        with patch("app.services.sources.indeed._scrape", side_effect=raise_error):
            from app.services.sources import indeed
            results = asyncio.run(indeed.fetch(query="SWE", location="NYC"))
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
