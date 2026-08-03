"""
Regression tests for the five source failures seen in production:
Arbeitnow's unfollowed 301 and self-inflicted 429, JSearch's 403-then-429
cascade, Indeed's retired RSS feed, and the two scrapers reporting nothing
useful when they find no cards.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.services.sources.base import SourceUnavailable, raise_if_blocked


def _resp(status: int = 200, json_data=None, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    resp.json.return_value = json_data if json_data is not None else {}
    resp.raise_for_status = MagicMock()
    return resp


class TestRaiseIfBlocked:
    @pytest.mark.parametrize("status", [401, 402, 403, 429])
    def test_blocking_statuses_stop_the_source(self, status):
        with pytest.raises(SourceUnavailable) as excinfo:
            raise_if_blocked(_resp(status), "Thing")
        assert str(status) in str(excinfo.value)

    @pytest.mark.parametrize("status", [200, 404, 500, 503])
    def test_other_statuses_pass_through(self, status):
        raise_if_blocked(_resp(status), "Thing")  # must not raise


class TestArbeitnow:
    @pytest.fixture(autouse=True)
    def _clear(self):
        from app.services.sources import arbeitnow
        arbeitnow.reset_cache()
        yield
        arbeitnow.reset_cache()

    def _feed_page(self, n: int):
        return {"data": [
            {"title": f"Backend Engineer {i}", "slug": f"job-{n}-{i}",
             "company_name": "Acme", "location": "Berlin", "remote": True,
             "url": f"https://arbeitnow.com/view/job-{n}-{i}",
             "description": "Python work.", "tags": ["python"],
             "created_at": 1754000000}
            for i in range(2)
        ]}

    def test_requests_the_www_host_and_follows_redirects(self):
        """The bare host 301s, and httpx does not follow redirects by default."""
        from app.services.sources import arbeitnow
        client = MagicMock()
        client.get.return_value = _resp(json_data=self._feed_page(1))
        ctx = MagicMock()
        ctx.__enter__.return_value = client

        with patch("app.services.sources.arbeitnow.httpx.Client", return_value=ctx) as factory:
            arbeitnow.fetch("Backend Engineer", "Berlin", max_pages=1)

        assert factory.call_args.kwargs["follow_redirects"] is True
        assert client.get.call_args[0][0].startswith("https://www.arbeitnow.com/")

    def test_the_feed_is_downloaded_once_across_many_queries(self):
        """Re-downloading the whole board per query/location earned a 429."""
        from app.services.sources import arbeitnow
        client = MagicMock()
        client.get.side_effect = [
            _resp(json_data=self._feed_page(1)),
            _resp(json_data={"data": []}),
        ]
        ctx = MagicMock()
        ctx.__enter__.return_value = client

        with patch("app.services.sources.arbeitnow.httpx.Client", return_value=ctx):
            for query in ("Backend Engineer", "Software Engineer", "Platform Engineer"):
                for loc in ("Berlin", "Remote"):
                    arbeitnow.fetch(query, loc, max_pages=2)

        # Two page requests total, not two per (query, location) pair.
        assert client.get.call_count == 2

    def test_a_rate_limit_stops_the_source(self):
        from app.services.sources import arbeitnow
        client = MagicMock()
        client.get.return_value = _resp(429)
        ctx = MagicMock()
        ctx.__enter__.return_value = client

        with patch("app.services.sources.arbeitnow.httpx.Client", return_value=ctx):
            with pytest.raises(SourceUnavailable):
                arbeitnow.fetch("Backend Engineer", "Berlin")

    def test_still_filters_by_query(self):
        from app.services.sources import arbeitnow
        client = MagicMock()
        client.get.side_effect = [
            _resp(json_data={"data": [
                {"title": "Backend Engineer", "slug": "a", "tags": ["python"],
                 "company_name": "Acme", "url": "u", "description": "d"},
                {"title": "Nurse Practitioner", "slug": "b", "tags": ["health"],
                 "company_name": "Clinic", "url": "u2", "description": "d"},
            ]}),
            _resp(json_data={"data": []}),
        ]
        ctx = MagicMock()
        ctx.__enter__.return_value = client

        with patch("app.services.sources.arbeitnow.httpx.Client", return_value=ctx):
            jobs = arbeitnow.fetch("Backend Engineer", "Berlin", max_pages=2)
        assert [j["title"] for j in jobs] == ["Backend Engineer"]


class TestJSearch:
    def test_a_403_stops_the_source_instead_of_returning_empty(self):
        """Swallowing this is what produced a 429 for every later combination."""
        from app.services.sources.jsearch import fetch
        with patch("httpx.get", return_value=_resp(403)):
            with pytest.raises(SourceUnavailable):
                fetch("key", "SWE", "United States")

    def test_a_429_stops_the_source(self):
        from app.services.sources.jsearch import fetch
        with patch("httpx.get", return_value=_resp(429)):
            with pytest.raises(SourceUnavailable):
                fetch("key", "SWE", "Canada")

    def test_a_transient_fault_still_yields_nothing_rather_than_raising(self):
        """Only "stop asking" propagates; a timeout keeps the old contract."""
        import httpx
        from app.services.sources.jsearch import fetch
        with patch("httpx.get", side_effect=httpx.HTTPError("timeout")):
            assert fetch("key", "SWE", "NYC") == []

    def test_a_good_response_still_parses(self):
        from app.services.sources.jsearch import fetch
        payload = {"data": [{"job_id": "1", "job_title": "Backend Engineer",
                             "employer_name": "Acme", "job_city": "NYC",
                             "job_state": "NY", "job_description": "Python.",
                             "job_apply_link": "https://x"}]}
        with patch("httpx.get", return_value=_resp(json_data=payload)):
            jobs = fetch("key", "SWE", "United States")
        assert jobs[0]["company"] == "Acme"
        assert jobs[0]["location"] == "NYC, NY"


class TestIndeedRetired:
    def test_a_404_is_reported_as_the_feed_being_gone(self):
        from app.services.sources.indeed import fetch
        with patch("httpx.get", return_value=_resp(404)):
            with pytest.raises(SourceUnavailable) as excinfo:
                fetch("SWE", "United States")
        assert "retired" in str(excinfo.value)

    def test_a_rate_limit_also_stops_it(self):
        from app.services.sources.indeed import fetch
        with patch("httpx.get", return_value=_resp(429)):
            with pytest.raises(SourceUnavailable):
                fetch("SWE", "United States")

    def test_a_transient_fault_still_yields_nothing_rather_than_raising(self):
        import httpx
        from app.services.sources.indeed import fetch
        with patch("httpx.get", side_effect=httpx.HTTPError("timeout")):
            assert fetch("SWE", "United States") == []

    def test_arbeitnow_transient_fault_yields_nothing(self):
        import httpx
        from app.services.sources import arbeitnow
        arbeitnow.reset_cache()
        with patch("app.services.sources.arbeitnow.httpx.Client",
                   side_effect=httpx.HTTPError("boom")):
            assert arbeitnow.fetch("SWE", "Berlin") == []


class TestOrchestratorStopsHammering:
    def test_one_block_abandons_the_remaining_combinations(self):
        """
        The production symptom: a 403 on the first query, then a 429 for every
        query after it. One refusal now ends the source for the cycle.
        """
        from app.services.job_fetcher import _run_combos
        calls = []

        def _fetch(role, loc):
            calls.append((role, loc))
            raise SourceUnavailable("JSearch returned HTTP 403")

        stats, jobs = {}, []
        _run_combos(stats, jobs, "jsearch", _fetch,
                    [(r, l) for r in ("a", "b", "c") for l in ("x", "y")])

        assert len(calls) == 1
        assert stats["jsearch"]["count"] == 0
        assert "403" in stats["jsearch"]["errors"][0]

    def test_an_ordinary_error_only_skips_that_combination(self):
        from app.services.job_fetcher import _run_combos
        calls = []

        def _fetch(role, loc):
            calls.append((role, loc))
            if role == "a":
                raise RuntimeError("transient")
            return [{"source": "s", "title": role}]

        stats, jobs = {}, []
        _run_combos(stats, jobs, "s", _fetch, [("a", "x"), ("b", "x")])

        assert len(calls) == 2
        assert len(jobs) == 1
        assert stats["s"]["count"] == 1

    def test_results_accumulate_normally(self):
        from app.services.job_fetcher import _run_combos
        stats, jobs = {}, []
        _run_combos(stats, jobs, "s",
                    lambda r, l: [{"source": "s", "title": f"{r}-{l}"}],
                    [("a", "x"), ("b", "y")])
        assert stats["s"]["count"] == 2
        assert stats["s"]["errors"] == []
        assert len(jobs) == 2


class TestDescribePage:
    async def _describe(self, title: str, body: str, url: str = "https://x/jobs"):
        from app.services.sources.playwright_base import describe_page
        page = MagicMock()
        page.url = url

        async def _title():
            return title

        async def _inner_text(_sel):
            return body

        page.title = _title
        page.inner_text = _inner_text
        return await describe_page(page)

    @pytest.mark.asyncio
    async def test_flags_a_bot_challenge(self):
        result = await self._describe("Just a moment...", "Checking your browser")
        assert "bot check" in result

    @pytest.mark.asyncio
    async def test_a_normal_page_is_not_flagged_as_blocked(self):
        result = await self._describe("Jobs at Acme", "Backend Engineer  Frontend Engineer")
        assert "bot check" not in result
        assert "Jobs at Acme" in result

    @pytest.mark.asyncio
    async def test_reports_url_and_size(self):
        result = await self._describe("T", "x" * 50, url="https://dice.com/jobs")
        assert "https://dice.com/jobs" in result
        assert "body_chars=50" in result

    @pytest.mark.asyncio
    async def test_survives_a_page_that_cannot_be_read(self):
        from app.services.sources.playwright_base import describe_page
        page = MagicMock()
        page.url = "https://x"

        async def _boom(*args):
            raise RuntimeError("detached")

        page.title = _boom
        page.inner_text = _boom
        result = await describe_page(page)
        assert "https://x" in result
