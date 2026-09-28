"""
Google Jobs through SerpApi: reading its results, spending its quota carefully,
and the settings that control both. No network — every request is answered at
`httpx.get`, which is where the adapter makes it.

The item shape follows SerpApi's documented Google Jobs response
(serpapi.com/google-jobs-api): `jobs_results[]` with `detected_extensions`,
`apply_options` and `job_id`, and `serpapi_pagination.next_page_token`.
"""

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.config import settings
from app.models.fetch_run import FetchRun, FetchSourceRun
from app.models.profile import Profile
from app.services import job_fetcher, tunables
from app.services.sources import google_jobs

KEY = "0123456789abcdef-test-key"


def item(n=1, title="Backend Engineer", company="Acme", location="Austin, TX",
         salary="120K–150K a year", posted="4 days ago", options=None, **extra):
    return {
        "title": title,
        "company_name": company,
        "location": location,
        "via": "LinkedIn",
        "share_link": f"https://www.google.com/search?ibp=htl;jobs&q=x&htidocid=doc{n}",
        "extensions": [posted],
        "detected_extensions": {"posted_at": posted, "salary": salary,
                                "schedule_type": "Full-time"},
        "description": "Build and run our Postgres-backed APIs.",
        "job_highlights": [{"title": "Qualifications", "items": ["3+ years"]}],
        "apply_options": options if options is not None else [
            {"title": "LinkedIn",
             "link": f"https://www.linkedin.com/jobs/view/{n}?utm_campaign=google_jobs_apply&utm_source=google_jobs_apply"},
            {"title": f"{company} Careers",
             "link": f"https://careers.acme.test/jobs/{n}?utm_source=google_jobs_apply&ref=g"},
        ],
        "job_id": f"eyJqb2JfdGl0bGUi{n}",
        **extra,
    }


def answer(items, token=None):
    body = {"search_metadata": {"status": "Success"}, "jobs_results": items}
    if token:
        body["serpapi_pagination"] = {"next_page_token": token}
    return body


class Serp:
    """A fake SerpApi: a handler per call, recording what was asked."""

    def __init__(self, monkeypatch, handler):
        self.calls: list[dict] = []
        self.handler = handler

        def get(url, params=None, **kw):
            assert url == "https://serpapi.com/search.json"
            self.calls.append(dict(params or {}))
            status, body = self.handler(dict(params or {}), len(self.calls))
            full = f"{url}?q=x&api_key={(params or {}).get('api_key')}"
            return httpx.Response(status, json=body, request=httpx.Request("GET", full))

        monkeypatch.setattr(httpx, "get", get)


class TestReadingAResult:
    def test_a_result_becomes_a_job(self, monkeypatch):
        Serp(monkeypatch, lambda p, n: (200, answer([item()])))
        (job,) = google_jobs.fetch_all(KEY, ["Backend Engineer"], ["Austin, TX"])
        assert (job["title"], job["company"], job["location"]) == \
            ("Backend Engineer", "Acme", "Austin, TX")
        assert job["source"] == "google_jobs"
        assert job["source_job_id"] == "eyJqb2JfdGl0bGUi1"
        assert job["employment_type"] == "full_time"
        assert job["description"].startswith("Build and run")
        posted = datetime.fromisoformat(job["posted_at"])
        assert timedelta(days=3.9) < datetime.now(timezone.utc) - posted < timedelta(days=4.1)

    def test_the_employers_own_link_wins_and_loses_its_tracking(self):
        job = google_jobs._as_job(item())
        assert job["url"] == "https://careers.acme.test/jobs/1?ref=g"

    def test_an_ats_link_beats_an_aggregator(self):
        job = google_jobs._as_job(item(options=[
            {"title": "Indeed", "link": "https://www.indeed.com/viewjob?jk=abc"},
            {"title": "Greenhouse", "link": "https://job-boards.greenhouse.io/acme/jobs/123"},
        ]))
        assert job["url"] == "https://job-boards.greenhouse.io/acme/jobs/123"

    def test_with_no_apply_links_googles_own_link_is_kept(self):
        job = google_jobs._as_job(item(options=[]))
        assert job["url"].startswith("https://www.google.com/search?ibp=htl;jobs")

    def test_us_pay_without_a_symbol_is_dollars(self):
        job = google_jobs._as_job(item())
        assert (job["salary_min"], job["salary_max"], job["salary_currency"],
                job["salary_period"]) == (120000, 150000, "USD", "year")

    def test_hourly_pay_keeps_its_period(self):
        job = google_jobs._as_job(item(salary="16.25–18.44 an hour"))
        assert (job["salary_min"], job["salary_max"], job["salary_period"]) == \
            (16.25, 18.44, "hour")

    @pytest.mark.parametrize("location", ["London, UK", "Anywhere", "Toronto, ON"])
    def test_pay_whose_currency_is_unknown_is_left_out(self, location):
        job = google_jobs._as_job(item(location=location))
        assert "salary_min" not in job and "salary_currency" not in job

    def test_a_printed_symbol_names_the_currency_anywhere(self):
        job = google_jobs._as_job(item(location="London, UK", salary="£60K–£75K a year"))
        assert (job["salary_min"], job["salary_currency"]) == (60000, "GBP")

    def test_up_to_is_a_ceiling_not_a_floor(self):
        job = google_jobs._as_job(item(salary="Up to 60 an hour"))
        assert job["salary_min"] is None and job["salary_max"] == 60

    def test_work_from_home_is_remote(self):
        remote = item(location="Anywhere")
        remote["detected_extensions"]["work_from_home"] = True
        assert google_jobs._as_job(remote)["is_remote"]
        assert not google_jobs._as_job(item())["is_remote"]

    def test_highlights_stand_in_for_a_missing_description(self):
        job = google_jobs._as_job(item(description=""))
        assert job["description"] == "3+ years"

    @pytest.mark.parametrize("role,location,text", [
        ("Backend Engineer", "Boston, MA", "Backend Engineer in Boston, MA"),
        ("Backend Engineer", "Remote", "Backend Engineer remote"),
        ("Backend Engineer", "", "Backend Engineer"),
    ])
    def test_the_place_goes_in_the_query(self, role, location, text):
        # SerpApi's own `location` refuses anything off Google's canonical
        # list, and a profile's locations are free text.
        assert google_jobs._query_text(role, location) == text


class TestTheSharedReaders:
    @pytest.mark.parametrize("location,expected", [
        ("Austin, TX", True), ("New York, NY 10001", True),
        ("Remote, United States", True), ("Seattle, WA, USA", True),
        ("Perth WA", False), ("Toronto, ON", False), ("London, UK", False),
        ("Bengaluru, Karnataka, IND", False), ("2 Locations", False),
        ("Anywhere", False), ("", False),
    ])
    def test_in_united_states_only_says_yes_when_the_string_does(self, location, expected):
        from app.services.sources.base import in_united_states

        assert in_united_states(location) is expected

    @pytest.mark.parametrize("text,days", [
        ("an hour ago", 1 / 24), ("a day ago", 1), ("18 hours ago", 18 / 24),
        ("30+ days ago", 30), ("2 months ago", 60), ("Just posted", 0),
    ])
    def test_relative_ages(self, text, days):
        from app.services.sources.base import posted_at_from_age

        now = datetime(2026, 9, 28, tzinfo=timezone.utc)
        assert posted_at_from_age(text, now=now) == (now - timedelta(days=days)).isoformat()

    def test_no_age_is_no_date(self):
        from app.services.sources.base import posted_at_from_age

        assert posted_at_from_age("") is None and posted_at_from_age(None) is None


class TestSpendingTheQuota:
    def test_first_pages_for_every_search_before_any_second_page(self, monkeypatch):
        def handler(params, n):
            return 200, answer([item(n)], token=f"tok-{params['q']}")
        serp = Serp(monkeypatch, handler)
        google_jobs.fetch_all(KEY, ["A", "B", "C"], ["Boston, MA"],
                              max_searches=4, pages=3)
        asked = [(c["q"], c.get("next_page_token")) for c in serp.calls]
        assert asked == [("A in Boston, MA", None), ("B in Boston, MA", None),
                         ("C in Boston, MA", None),
                         ("A in Boston, MA", "tok-A in Boston, MA")]

    def test_the_budget_is_a_hard_cap_and_says_so(self, monkeypatch, caplog):
        serp = Serp(monkeypatch, lambda p, n: (200, answer([item(n)])))
        with caplog.at_level(logging.WARNING, logger=google_jobs.__name__):
            jobs = google_jobs.fetch_all(KEY, ["A", "B", "C"], ["X", "Y"], max_searches=2)
        assert len(serp.calls) == 2 and len(jobs) == 2
        assert "budget" in caplog.text

    def test_no_warning_when_the_budget_covered_everything(self, monkeypatch, caplog):
        Serp(monkeypatch, lambda p, n: (200, answer([item(n)])))
        with caplog.at_level(logging.WARNING, logger=google_jobs.__name__):
            google_jobs.fetch_all(KEY, ["A", "B"], ["X"], max_searches=2)
        assert "budget" not in caplog.text

    def test_paging_stops_when_google_has_no_more(self, monkeypatch):
        serp = Serp(monkeypatch, lambda p, n: (200, answer([item(n)])))
        google_jobs.fetch_all(KEY, ["A"], ["X"], max_searches=10, pages=5)
        assert len(serp.calls) == 1


class TestWhenSerpApiRefuses:
    def test_a_rejected_key_stops_the_run_and_keeps_what_was_found(self, monkeypatch, caplog):
        def handler(params, n):
            if n == 1:
                return 200, answer([item(1)])
            return 401, {"error": "Invalid API key."}
        serp = Serp(monkeypatch, handler)
        with caplog.at_level(logging.ERROR, logger=google_jobs.__name__):
            jobs = google_jobs.fetch_all(KEY, ["A", "B", "C"], ["X"])
        assert len(jobs) == 1 and len(serp.calls) == 2
        assert "401" in caplog.text

    def test_a_spent_quota_answered_in_the_body_stops_too(self, monkeypatch):
        serp = Serp(monkeypatch, lambda p, n: (
            200, {"error": "Your account has run out of searches."}))
        assert google_jobs.fetch_all(KEY, ["A", "B"], ["X"]) == []
        assert len(serp.calls) == 1

    def test_no_results_is_not_an_error(self, monkeypatch, caplog):
        serp = Serp(monkeypatch, lambda p, n: (
            200, {"error": "Google hasn't returned any results for this query."}))
        with caplog.at_level(logging.ERROR, logger=google_jobs.__name__):
            assert google_jobs.fetch_all(KEY, ["A", "B"], ["X"]) == []
        assert len(serp.calls) == 2
        assert caplog.text == ""

    def test_the_key_never_reaches_a_log_line(self, monkeypatch, caplog):
        # httpx names the URL in its errors, and the key is a query parameter.
        Serp(monkeypatch, lambda p, n: (500, {"oops": True}))
        with caplog.at_level(logging.ERROR, logger=google_jobs.__name__):
            google_jobs.fetch_all(KEY, ["A"], ["X"])
        assert "500" in caplog.text
        assert KEY not in caplog.text


def _history(db, source, hours_ago, status="ok"):
    run = FetchRun(started_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
                   group="api")
    run.sources.append(FetchSourceRun(source=source, status=status,
                                      enabled=status != "disabled"))
    db.add(run)
    db.commit()


class TestTheFetcherAndTheSettingsPage:
    """Overrides stored on the profile change what the fetcher does."""

    def _cfg(self, db, overrides):
        db.query(Profile).delete()
        db.add(Profile(data={tunables.STORE_KEY: overrides}))
        db.commit()
        return tunables.effective_settings(db.query(Profile).first().data)

    def _run(self, cfg, **kw):
        return job_fetcher._run_all_adapters(
            ["A", "B", "C"], ["Boston, MA"], cfg, {}, {}, only={"google_jobs"}, **kw)

    def test_searches_per_run(self, db, monkeypatch):
        monkeypatch.setattr(settings, "SERPAPI_API_KEY", KEY)
        serp = Serp(monkeypatch, lambda p, n: (200, answer([item(n)])))
        self._run(self._cfg(db, {"google_jobs_max_searches": 1}))
        assert len(serp.calls) == 1
        serp.calls.clear()
        self._run(self._cfg(db, {"google_jobs_max_searches": 3}))
        assert len(serp.calls) == 3

    def test_pages_per_search(self, db, monkeypatch):
        monkeypatch.setattr(settings, "SERPAPI_API_KEY", KEY)
        serp = Serp(monkeypatch, lambda p, n: (200, answer([item(n)], token=f"t{n}")))
        self._run(self._cfg(db, {"google_jobs_max_searches": 50, "google_jobs_pages": 2}))
        assert len(serp.calls) == 6

    def test_switching_it_off(self, db, monkeypatch):
        monkeypatch.setattr(settings, "SERPAPI_API_KEY", KEY)
        serp = Serp(monkeypatch, lambda p, n: (200, answer([])))
        _, stats = self._run(self._cfg(db, {"google_jobs_enabled": False}))
        assert serp.calls == [] and stats["google_jobs"]["enabled"] is False

    def test_without_a_key_nothing_is_asked(self, db, monkeypatch):
        monkeypatch.setattr(settings, "SERPAPI_API_KEY", "")
        serp = Serp(monkeypatch, lambda p, n: (200, answer([])))
        _, stats = self._run(self._cfg(db, {}))
        assert serp.calls == [] and stats["google_jobs"]["enabled"] is False

    def test_the_interval_holds_a_scheduled_run_back(self, db, monkeypatch):
        monkeypatch.setattr(settings, "SERPAPI_API_KEY", KEY)
        serp = Serp(monkeypatch, lambda p, n: (200, answer([item(n)])))
        _history(db, "google_jobs", hours_ago=3)
        cfg = self._cfg(db, {"google_jobs_interval_hours": 24})
        waiting = job_fetcher._sources_not_due(db, cfg)
        assert "next in about 21h" in waiting["google_jobs"]
        _, stats = self._run(cfg, not_due=waiting, manual=False)
        assert serp.calls == []
        assert stats["google_jobs"]["enabled"] is False
        assert "at most every 24h" in stats["google_jobs"]["errors"][0]

    def test_a_shorter_interval_on_the_settings_page_lets_it_run(self, db, monkeypatch):
        _history(db, "google_jobs", hours_ago=3)
        assert job_fetcher._sources_not_due(db, self._cfg(db, {"google_jobs_interval_hours": 2})) == {}

    def test_a_manual_run_ignores_the_interval(self, db, monkeypatch):
        monkeypatch.setattr(settings, "SERPAPI_API_KEY", KEY)
        serp = Serp(monkeypatch, lambda p, n: (200, answer([item(n)])))
        _history(db, "google_jobs", hours_ago=1)
        cfg = self._cfg(db, {})
        self._run(cfg, not_due=job_fetcher._sources_not_due(db, cfg), manual=True)
        assert serp.calls

    def test_a_skipped_run_does_not_reset_the_clock(self, db):
        # Skips are recorded as disabled. Counting them as "ran" would push
        # the next real run back forever.
        _history(db, "google_jobs", hours_ago=30)
        _history(db, "google_jobs", hours_ago=1, status="disabled")
        assert job_fetcher._sources_not_due(db, self._cfg(db, {})) == {}

    def test_it_is_part_of_the_api_group(self):
        assert "google_jobs" in job_fetcher.SOURCE_GROUPS["api"]
