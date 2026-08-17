"""
The sources added in Phase 2.2, and what happens to a source that has died.

USAJOBS is the outlier worth stating: it is the only source that states pay on
every posting, because federal salary ranges are public by law. Those go
straight into the salary columns rather than being re-derived from prose by a
model call later — which is also the first time an adapter has been able to
hand structured details to the fetcher at all.
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from app.models.job import Job, JobStatus


def _resp(payload=None, text="", status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload if payload is not None else {}
    resp.text = text
    resp.content = text.encode()
    resp.headers = {"content-type": "application/json"}
    resp.raise_for_status = MagicMock()
    return resp


def _usajobs_payload(items):
    return {"SearchResult": {"SearchResultItems": items}}


def _usajobs_item(**overrides):
    descriptor = {
        "PositionTitle": "IT Specialist (Software Engineer)",
        "PositionURI": "https://www.usajobs.gov/job/123456700",
        "OrganizationName": "Department of Energy",
        "PositionLocationDisplay": "Washington, DC",
        "PublicationStartDate": "2026-08-01",
        "PositionSchedule": [{"Name": "Full-Time"}],
        "PositionRemuneration": [
            {"MinimumRange": "117962", "MaximumRange": "153354",
             "RateIntervalCode": "PA"}
        ],
        "QualificationSummary": "You will build software.",
        "UserArea": {"Details": {
            "JobSummary": "<p>Work on national systems.</p>",
            "MajorDuties": ["<ul><li>Python</li><li>Go</li></ul>"],
        }},
    }
    descriptor.update(overrides)
    return {"MatchedObjectId": "123456700", "MatchedObjectDescriptor": descriptor}


class TestUSAJobsAdapter:
    def test_returns_standard_dicts(self):
        from app.services.sources.usajobs import fetch
        with patch("httpx.get", return_value=_resp(_usajobs_payload([_usajobs_item()]))):
            results = fetch(api_key="K", user_agent="me@example.com",
                            query="Software Engineer", location="DC")

        assert len(results) == 1
        job = results[0]
        assert job["source"] == "usajobs"
        assert job["source_job_id"] == "123456700"
        assert job["company"] == "Department of Energy"
        assert job["location"] == "Washington, DC"
        assert job["url"] == "https://www.usajobs.gov/job/123456700"

    def test_the_stated_salary_lands_in_the_columns(self):
        from app.services.sources.usajobs import fetch
        with patch("httpx.get", return_value=_resp(_usajobs_payload([_usajobs_item()]))):
            job = fetch(api_key="K", user_agent="me@example.com",
                        query="SWE", location="")[0]
        assert job["salary_min"] == 117962
        assert job["salary_max"] == 153354
        assert job["salary_currency"] == "USD"
        assert job["employment_type"] == "full_time"

    def test_an_hourly_rate_is_left_null_rather_than_annualised(self):
        """
        The posting did not state a yearly figure. Inventing one would put a
        number nobody wrote down in front of the salary filter.
        """
        from app.services.sources.usajobs import fetch
        hourly = _usajobs_item(PositionRemuneration=[
            {"MinimumRange": "35", "MaximumRange": "45", "RateIntervalCode": "PH"}
        ])
        with patch("httpx.get", return_value=_resp(_usajobs_payload([hourly]))):
            job = fetch(api_key="K", user_agent="me@example.com",
                        query="SWE", location="")[0]
        assert "salary_min" not in job

    def test_the_duties_are_part_of_the_description(self):
        # The summary alone is framing; the duties are what the skill filter
        # and the matcher are reading for.
        from app.services.sources.usajobs import fetch
        with patch("httpx.get", return_value=_resp(_usajobs_payload([_usajobs_item()]))):
            job = fetch(api_key="K", user_agent="me@example.com",
                        query="SWE", location="")[0]
        assert "You will build software." in job["description"]
        assert "Work on national systems." in job["description"]
        assert "- Python" in job["description"]
        assert "<p>" not in job["description"]

    def test_a_rejected_key_stops_the_source_rather_than_every_query(self):
        from app.services.sources.base import SourceUnavailable
        from app.services.sources.usajobs import fetch
        import pytest

        with patch("httpx.get", return_value=_resp(status=401)):
            with pytest.raises(SourceUnavailable):
                fetch(api_key="bad", user_agent="me@example.com",
                      query="SWE", location="")

    def test_rows_without_a_title_or_url_are_dropped(self):
        from app.services.sources.usajobs import fetch
        broken = _usajobs_item(PositionTitle="", PositionURI="")
        with patch("httpx.get", return_value=_resp(
            _usajobs_payload([broken, _usajobs_item()])
        )):
            results = fetch(api_key="K", user_agent="me@example.com",
                            query="SWE", location="")
        assert len(results) == 1

    def test_a_transient_error_yields_nothing_rather_than_raising(self):
        import httpx
        from app.services.sources.usajobs import fetch
        with patch("httpx.get", side_effect=httpx.ConnectError("boom")):
            assert fetch(api_key="K", user_agent="me@example.com",
                         query="SWE", location="") == []


class TestHiringCafeAdapter:
    """
    Read by shape rather than by path: the endpoint is undocumented, and a
    redesign that moves the nesting should keep working.
    """

    _PAYLOAD = {"hits": {"results": [
        {"id": 998877, "jobTitle": "Backend Engineer",
         "companyName": "Acme", "formattedLocation": "Remote",
         "applyUrl": "https://boards.greenhouse.io/acme/jobs/998877",
         "description": "<p>Build APIs with Python.</p>"},
    ]}}

    def test_a_job_is_found_wherever_it_is_nested(self):
        from app.services.sources.hiringcafe import fetch
        with patch("httpx.post", return_value=_resp(self._PAYLOAD)):
            results = fetch(query="Backend Engineer", location="Remote")

        assert len(results) == 1
        job = results[0]
        assert job["source"] == "hiringcafe"
        assert job["title"] == "Backend Engineer"
        assert job["company"] == "Acme"
        assert job["url"] == "https://boards.greenhouse.io/acme/jobs/998877"
        assert job["description"] == "Build APIs with Python."

    def test_a_deeper_nesting_still_works(self):
        from app.services.sources.hiringcafe import fetch
        moved = {"data": {"page": {"items": [{"node": self._PAYLOAD["hits"]["results"][0]}]}}}
        with patch("httpx.post", return_value=_resp(moved)):
            results = fetch(query="Backend Engineer")
        assert len(results) == 1

    def test_an_unrecognisable_payload_says_so(self, caplog):
        import logging
        from app.services.sources.hiringcafe import fetch
        with caplog.at_level(logging.WARNING):
            with patch("httpx.post", return_value=_resp({"totally": "different"})):
                results = fetch(query="Backend Engineer")
        assert results == []
        assert "no job-shaped objects" in caplog.text

    def test_a_blocked_response_stops_the_source(self):
        import pytest
        from app.services.sources.base import SourceUnavailable
        from app.services.sources.hiringcafe import fetch

        with patch("httpx.post", return_value=_resp(status=429)):
            with pytest.raises(SourceUnavailable):
                fetch(query="Backend Engineer")

    def test_no_linkedin_url_is_ever_invented_for_it(self):
        """
        The harvest reader reconstructs a linkedin.com URL from a bare id.
        That must not happen for another source's ids.
        """
        from app.services.sources.hiringcafe import fetch
        no_url = {"results": [{"id": 555444333, "jobTitle": "Backend Engineer",
                               "companyName": "Acme"}]}
        with patch("httpx.post", return_value=_resp(no_url)):
            results = fetch(query="Backend Engineer")
        assert results == []


class TestYCombinatorAdapter:
    def _page(self, title="Backend Engineer", url="https://www.ycombinator.com/companies/acme/jobs/1"):
        import json as _json
        node = _json.dumps({
            "@type": "JobPosting", "title": title, "url": url,
            "hiringOrganization": {"name": "Acme (YC W21)"},
            "jobLocation": {"address": {"addressLocality": "San Francisco",
                                        "addressRegion": "CA"}},
        })
        return ("<html><script type=\"application/ld+json\">[" + node
                + "]</script>" + ("x" * 3000) + "</html>")

    def test_role_pages_are_read(self):
        from app.services.sources.ycombinator import fetch
        with patch("httpx.get", return_value=_resp(text=self._page())):
            results = fetch(roles=["software-engineer"])

        assert len(results) == 1
        assert results[0]["source"] == "ycombinator"
        assert results[0]["company"] == "Acme (YC W21)"

    def test_the_same_posting_across_role_pages_is_counted_once(self):
        from app.services.sources.ycombinator import fetch
        with patch("httpx.get", return_value=_resp(text=self._page())):
            results = fetch(roles=["software-engineer", "backend-engineer"])
        assert len(results) == 1

    def test_one_failing_role_page_does_not_cost_the_others(self):
        import httpx
        from app.services.sources.ycombinator import fetch
        calls = []

        def _get(url, **kwargs):
            calls.append(url)
            if "backend" in url:
                raise httpx.HTTPError("500")
            return _resp(text=self._page())

        with patch("httpx.get", side_effect=_get):
            results = fetch(roles=["backend-engineer", "software-engineer"])
        assert len(calls) == 2
        assert len(results) == 1


class TestRestingDeadSources:
    """
    JSearch has 403'd on every run for twenty runs — an expired key answers
    identically forever, and calling it each cycle buys an error line that
    trains everyone to ignore error lines.
    """

    def _run(self, db, statuses: dict, started_at):
        from app.models.fetch_run import FetchRun, FetchSourceRun

        run = FetchRun(started_at=started_at, status="ok")
        db.add(run)
        db.flush()
        for source, status in statuses.items():
            db.add(FetchSourceRun(run_id=run.id, source=source, status=status,
                                  enabled=status != "disabled"))
        return run

    def _history(self, db, count: int, status: str = "failed", source="jsearch"):
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        for i in range(count):
            self._run(db, {source: status, "adzuna": "ok"},
                      now - timedelta(hours=count - i))
        db.commit()

    def test_a_long_failing_streak_is_counted(self, db):
        from app.services.fetch_history import failing_streaks

        self._history(db, 12)
        streaks = failing_streaks(db)
        assert streaks["jsearch"] == 12
        assert "adzuna" not in streaks

    def test_one_success_ends_the_streak(self, db):
        from datetime import timedelta
        from app.services.fetch_history import failing_streaks

        now = datetime.now(timezone.utc)
        for i in range(12):
            self._run(db, {"jsearch": "failed"}, now - timedelta(hours=20 - i))
        self._run(db, {"jsearch": "ok"}, now)  # newest
        db.commit()

        assert failing_streaks(db).get("jsearch", 0) == 0

    def test_a_long_streak_rests_the_source(self, db):
        from app.services.fetch_history import resting_sources

        self._history(db, 12)
        # retry_every=0 disables the periodic probe, isolating the rest rule.
        assert resting_sources(db, threshold=10, retry_every=0) == {"jsearch": 12}

    def test_a_short_streak_does_not(self, db):
        from app.services.fetch_history import resting_sources

        self._history(db, 3)
        assert resting_sources(db, threshold=10, retry_every=0) == {}

    def test_a_probe_still_goes_out_periodically(self, db):
        """
        Resting is not removal: a key the user refreshes has to resume without
        anybody remembering to re-enable anything.
        """
        from app.services.fetch_history import resting_sources

        self._history(db, 10)  # 10 runs recorded, so 10 % 10 == 0
        assert resting_sources(db, threshold=5, retry_every=10) == {}

    def test_a_resting_source_is_not_called(self, db):
        from app.services.job_fetcher import _run_all_adapters
        from app.config import settings

        # A source with no key reports "no key" rather than "resting", which
        # is the more useful of the two answers — so give it one.
        with patch("app.services.sources.jsearch.fetch") as jsearch, \
             patch.object(settings, "JSEARCH_API_KEY", "key"):
            _, stats = _run_all_adapters(
                ["SWE"], ["NYC"], settings, ats_slugs={},
                resting={"jsearch": 20},
            )

        jsearch.assert_not_called()
        assert stats["jsearch"]["enabled"] is False
        assert "resting after failing 20 runs" in stats["jsearch"]["errors"][0]

    def test_an_explicit_manual_trigger_still_runs_a_rested_source(self, db):
        # Resting is an automatic economy, not a lock. Asking for it by name
        # on the runs page has to actually ask for it.
        from app.services.job_fetcher import _run_all_adapters
        from app.config import settings

        with patch("app.services.sources.jsearch.fetch", return_value=[]) as jsearch, \
             patch.object(settings, "JSEARCH_API_KEY", "key"):
            _run_all_adapters(
                ["SWE"], ["NYC"], settings, ats_slugs={},
                only={"jsearch"}, resting={"jsearch": 20},
            )
        jsearch.assert_called()


class TestAdapterSuppliedDetails:
    """
    An adapter handed structured pay used to have it thrown away and re-derived
    from prose by a model call. USAJOBS states it on every posting.
    """

    def _profile(self, db):
        from app.models.profile import Profile
        db.add(Profile(data={"target_roles": ["Backend Engineer"], "skills": {}}))
        db.commit()

    def test_stated_pay_reaches_the_stored_job(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs

        self._profile(db)
        raw = [{
            "source": "usajobs", "source_job_id": "1", "title": "Backend Engineer",
            "company": "DOE", "location": "DC", "url": "https://usajobs.gov/job/1",
            "description": "Build things.",
            "salary_min": 117962.0, "salary_max": 153354.0,
            "salary_currency": "USD", "employment_type": "full_time",
        }]
        with patch("app.services.job_fetcher._run_all_adapters",
                   return_value=(raw, {"usajobs": {"count": 1, "errors": [],
                                                   "enabled": True}})):
            fetch_and_save_jobs(db)

        job = db.query(Job).filter(Job.source_job_id == "1").one()
        assert job.salary_label == "$117.962k–$153.354k"
        assert job.employment_type == "full_time"

    def test_a_source_that_states_nothing_leaves_the_columns_null(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs

        self._profile(db)
        raw = [{
            "source": "remotive", "source_job_id": "2", "title": "Backend Engineer",
            "company": "Acme", "location": "Remote", "url": "https://x/2",
            "description": "Build things.",
        }]
        with patch("app.services.job_fetcher._run_all_adapters",
                   return_value=(raw, {"remotive": {"count": 1, "errors": [],
                                                    "enabled": True}})):
            fetch_and_save_jobs(db)

        job = db.query(Job).filter(Job.source_job_id == "2").one()
        assert job.salary_min is None
        assert job.employment_type is None

    def test_a_currency_without_an_amount_is_not_stored_as_stated_pay(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs

        self._profile(db)
        raw = [{
            "source": "teamtailor", "source_job_id": "3", "title": "Backend Engineer",
            "company": "Acme", "location": "Remote", "url": "https://x/3",
            "description": "Build things.", "salary_currency": "USD",
        }]
        with patch("app.services.job_fetcher._run_all_adapters",
                   return_value=(raw, {"teamtailor": {"count": 1, "errors": [],
                                                      "enabled": True}})):
            fetch_and_save_jobs(db)

        job = db.query(Job).filter(Job.source_job_id == "3").one()
        assert job.salary_currency is None

    def test_a_later_source_fills_a_gap_the_first_one_left(self, db):
        """
        One source's listing carries pay and another's doesn't; the one that
        does should win over whichever happened to be fetched first.
        """
        from app.services.job_fetcher import fetch_and_save_jobs

        self._profile(db)
        from app.services.deduplication import compute_dedupe_hash

        existing = Job(
            source="adzuna", source_job_id="9", source_urls=["https://x/9"],
            title="Backend Engineer", company="Acme", location="Remote",
            url="https://x/9", status=JobStatus.new,
            fetched_at=datetime.now(timezone.utc),
            # The real hash, so the cross-post actually collides with it —
            # which is the path the backfill lives on.
            dedupe_hash=compute_dedupe_hash("Acme", "Backend Engineer", "Remote"),
        )
        db.add(existing)
        db.commit()

        raw = [{
            "source": "usajobs", "source_job_id": "10", "title": "Backend Engineer",
            "company": "Acme", "location": "Remote", "url": "https://x/10",
            "description": "Build things.",
            "salary_min": 120000.0, "salary_max": 160000.0, "salary_currency": "USD",
        }]
        with patch("app.services.job_fetcher._run_all_adapters",
                   return_value=(raw, {"usajobs": {"count": 1, "errors": [],
                                                   "enabled": True}})):
            fetch_and_save_jobs(db)

        db.refresh(existing)
        assert existing.salary_min == 120000.0


class TestTheyAreWiredUp:
    NEW = ("usajobs", "hiringcafe", "ycombinator")

    def test_each_can_be_triggered_from_the_runs_page(self):
        from app.routers.runs import TRIGGERABLE_SOURCES
        for source in self.NEW:
            assert source in TRIGGERABLE_SOURCES, source

    def test_usajobs_reports_what_it_needs_when_unconfigured(self):
        from app.config import settings
        from app.services.job_fetcher import _run_all_adapters

        with patch.object(settings, "USAJOBS_API_KEY", ""), \
             patch.object(settings, "USAJOBS_USER_AGENT", ""):
            _, stats = _run_all_adapters(["SWE"], ["NYC"], settings, ats_slugs={},
                                         only={"usajobs"})
        assert stats["usajobs"]["enabled"] is False
