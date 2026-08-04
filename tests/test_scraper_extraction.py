"""
Tests for the two scraper fixes the page diagnostics pointed at.

Dice loaded a real page (title 'Search Jobs | Dice.com', 2000+ chars of body)
but the <dhi-job-card> element it keyed on no longer exists — a markup change,
so extraction was rewritten to be markup-independent. Wellfound rendered a body
of exactly zero characters, which is not a selector problem at all.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _page(*, evaluate=None, title="Search Jobs | Dice.com", body="Job Search",
          selector_ok=True, url="https://www.dice.com/jobs"):
    page = MagicMock()
    page.url = url
    page.goto = AsyncMock()
    page.wait_for_load_state = AsyncMock()
    page.wait_for_selector = (
        AsyncMock() if selector_ok else AsyncMock(side_effect=RuntimeError("timeout"))
    )
    page.wait_for_function = AsyncMock()
    page.evaluate = AsyncMock(return_value=evaluate if evaluate is not None else [])
    page.title = AsyncMock(return_value=title)
    page.inner_text = AsyncMock(return_value=body)
    return page


def _playwright(page):
    """Patch target for async_playwright() so no browser is launched."""
    browser = MagicMock()
    browser.close = AsyncMock()
    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)
    browser.new_context = AsyncMock(return_value=context)
    chromium = MagicMock()
    chromium.launch = AsyncMock(return_value=browser)
    pw = MagicMock()
    pw.chromium = chromium
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=pw)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


class TestDiceExtraction:
    _ROW = {
        "title": "Backend Engineer",
        "company": "Acme",
        "location": "New York, NY",
        "url": "https://www.dice.com/job-detail/abc-123",
        "description": "Python and Go.",
    }

    @pytest.mark.asyncio
    async def test_builds_jobs_from_whatever_the_extractor_returns(self):
        from app.services.sources.dice import fetch
        page = _page(evaluate=[self._ROW])
        with patch("playwright.async_api.async_playwright", return_value=_playwright(page)):
            jobs = await fetch("Backend Engineer", "United States")

        assert len(jobs) == 1
        job = jobs[0]
        assert job["source"] == "dice"
        assert job["company"] == "Acme"
        assert job["url"] == "https://www.dice.com/job-detail/abc-123"
        assert job["description"] == "Python and Go."

    @pytest.mark.asyncio
    async def test_a_missing_card_selector_no_longer_aborts_the_scrape(self):
        """
        The actual regression: the selector timed out, so extraction never ran
        even though the page was fine and carried structured data.
        """
        from app.services.sources.dice import fetch
        page = _page(evaluate=[self._ROW], selector_ok=False)
        with patch("playwright.async_api.async_playwright", return_value=_playwright(page)):
            jobs = await fetch("Backend Engineer", "United States")

        page.evaluate.assert_awaited()
        assert len(jobs) == 1

    @pytest.mark.asyncio
    async def test_reports_the_page_when_nothing_is_extractable(self):
        from app.services.sources.dice import fetch
        page = _page(evaluate=[])
        with patch("playwright.async_api.async_playwright", return_value=_playwright(page)):
            jobs = await fetch("Backend Engineer", "United States")
        assert jobs == []
        page.title.assert_awaited()  # diagnostics were gathered

    @pytest.mark.asyncio
    async def test_rows_without_a_title_are_dropped(self):
        from app.services.sources.dice import fetch
        page = _page(evaluate=[{"title": "  ", "url": "u"}, self._ROW])
        with patch("playwright.async_api.async_playwright", return_value=_playwright(page)):
            jobs = await fetch("Backend Engineer", "United States")
        assert len(jobs) == 1

    @pytest.mark.asyncio
    async def test_remote_is_inferred(self):
        from app.services.sources.dice import fetch
        row = {**self._ROW, "location": "Remote"}
        page = _page(evaluate=[row])
        with patch("playwright.async_api.async_playwright", return_value=_playwright(page)):
            jobs = await fetch("Backend Engineer", "United States")
        assert jobs[0]["is_remote"] is True

    @pytest.mark.asyncio
    async def test_an_extraction_crash_yields_nothing_rather_than_raising(self):
        from app.services.sources.dice import fetch
        page = _page()
        page.evaluate = AsyncMock(side_effect=RuntimeError("JS blew up"))
        with patch("playwright.async_api.async_playwright", return_value=_playwright(page)):
            jobs = await fetch("Backend Engineer", "United States")
        assert jobs == []

    def test_the_extractor_covers_all_three_strategies(self):
        """JSON-LD, embedded app state, then job-detail links."""
        from app.services.sources.dice import _EXTRACT_JS
        assert "application/ld+json" in _EXTRACT_JS
        assert "__NEXT_DATA__" in _EXTRACT_JS
        assert "/job-detail/" in _EXTRACT_JS

    def test_the_ready_selector_no_longer_depends_only_on_the_dead_element(self):
        from app.services.sources.dice import _READY_SELECTOR
        assert "job-detail" in _READY_SELECTOR
        assert "ld+json" in _READY_SELECTOR


class TestWellfoundRolePages:
    """
    The /jobs?q= search route rendered zero characters from a server. The
    /role/<slug> landing pages are server-rendered, so that's what we scrape.
    """

    @pytest.fixture(autouse=True)
    def _clear(self):
        from app.services.sources import wellfound
        wellfound.reset_cache()
        yield
        wellfound.reset_cache()

    _ROW = {
        "title": "Software Engineer",
        "company": "Acme",
        "location": "San Francisco, CA",
        "url": "https://wellfound.com/jobs/12345-software-engineer",
        "description": "Build things.",
        "remote": False,
    }

    def test_the_role_slug_is_built_from_the_query(self):
        from app.services.sources.wellfound import role_slug
        assert role_slug("Software Engineer") == "software-engineer"
        assert role_slug("Senior  Back-End Engineer!") == "senior-back-end-engineer"
        assert role_slug("") == "software-engineer"

    @pytest.mark.asyncio
    async def test_it_requests_the_role_landing_page(self):
        from app.services.sources.wellfound import fetch
        page = _page(evaluate=[self._ROW], title="Software Engineer Jobs",
                     body="x" * 500)
        with patch("playwright.async_api.async_playwright", return_value=_playwright(page)):
            await fetch("Software Engineer", "United States")
        assert page.goto.call_args[0][0] == "https://wellfound.com/role/software-engineer"

    def test_the_configured_roles_default_to_the_four_we_want(self):
        from app.services.sources.wellfound import configured_roles
        assert configured_roles() == [
            "software-engineer", "full-stack-engineer",
            "backend-engineer", "mobile-engineer",
        ]

    def test_configured_roles_are_slugified_and_overridable(self):
        from app.config import settings
        from app.services.sources.wellfound import configured_roles
        with patch.object(settings, "WELLFOUND_ROLES", "Data Engineer, ml-engineer"):
            assert configured_roles() == ["data-engineer", "ml-engineer"]

    def test_a_blank_setting_falls_back_to_the_defaults(self):
        from app.config import settings
        from app.services.sources.wellfound import configured_roles, DEFAULT_ROLES
        with patch.object(settings, "WELLFOUND_ROLES", ""):
            assert configured_roles() == list(DEFAULT_ROLES)

    @pytest.mark.asyncio
    async def test_every_configured_role_page_is_visited(self):
        from app.services.sources.wellfound import fetch_roles
        page = _page(evaluate=[self._ROW], body="x" * 500)
        with patch("playwright.async_api.async_playwright", return_value=_playwright(page)):
            await fetch_roles()

        visited = [c[0][0] for c in page.goto.call_args_list]
        assert visited == [
            "https://wellfound.com/role/software-engineer",
            "https://wellfound.com/role/full-stack-engineer",
            "https://wellfound.com/role/backend-engineer",
            "https://wellfound.com/role/mobile-engineer",
        ]

    @pytest.mark.asyncio
    async def test_all_roles_share_one_browser_launch(self):
        """Starting Chromium dominates this source's cost."""
        from app.services.sources.wellfound import fetch_roles
        page = _page(evaluate=[self._ROW], body="x" * 500)
        ctx = _playwright(page)
        with patch("playwright.async_api.async_playwright", return_value=ctx):
            await fetch_roles()
        chromium = ctx.__aenter__.return_value.chromium
        assert chromium.launch.await_count == 1

    @pytest.mark.asyncio
    async def test_one_broken_role_does_not_lose_the_others(self):
        from app.services.sources.wellfound import fetch_roles
        page = _page(evaluate=[self._ROW], body="x" * 500)
        calls = {"n": 0}

        async def _goto(url, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("navigation failed")
            return MagicMock(status=200)

        page.goto = AsyncMock(side_effect=_goto)
        with patch("playwright.async_api.async_playwright", return_value=_playwright(page)):
            jobs = await fetch_roles(["a-role", "broken-role", "c-role"])
        # Three attempted, the middle one empty, the others still counted.
        assert page.goto.await_count == 3
        assert len(jobs) == 2

    def _html_resp(self, html: str, status: int = 200) -> MagicMock:
        resp = MagicMock()
        resp.status_code = status
        resp.text = html
        return resp

    _LD_HTML = """<html><head>
      <script type="application/ld+json">
      {"@type": "JobPosting", "title": "Backend Engineer",
       "url": "https://wellfound.com/jobs/99-backend",
       "hiringOrganization": {"name": "Acme"},
       "jobLocation": {"address": {"addressLocality": "Remote",
                                   "addressRegion": ""}},
       "description": "<p>Go and Postgres.</p>"}
      </script></head><body>x</body></html>"""

    @pytest.mark.asyncio
    async def test_plain_http_is_tried_before_launching_a_browser(self):
        """
        Headless Chromium gets an empty response from Wellfound. A plain client
        has a different TLS fingerprint and these pages are server-rendered, so
        it's worth trying — and it costs nothing when it works.
        """
        from app.services.sources.wellfound import fetch_roles
        with patch("httpx.get", return_value=self._html_resp(self._LD_HTML)), \
             patch("playwright.async_api.async_playwright") as pw:
            jobs = await fetch_roles(["backend-engineer"])

        pw.assert_not_called()
        assert len(jobs) == 1
        assert jobs[0]["title"] == "Backend Engineer"
        assert jobs[0]["company"] == "Acme"
        assert "Go and Postgres" in jobs[0]["description"]

    @pytest.mark.asyncio
    async def test_it_falls_back_to_the_browser_when_plain_http_gives_nothing(self):
        from app.services.sources.wellfound import fetch_roles
        page = _page(evaluate=[self._ROW], body="x" * 500)
        with patch("httpx.get", return_value=self._html_resp("<html></html>")), \
             patch("playwright.async_api.async_playwright",
                   return_value=_playwright(page)):
            jobs = await fetch_roles(["backend-engineer"])

        page.goto.assert_awaited()
        assert len(jobs) == 1

    @pytest.mark.asyncio
    async def test_a_blocked_plain_http_response_falls_through_quietly(self):
        from app.services.sources.wellfound import fetch_roles
        page = _page(evaluate=[self._ROW], body="x" * 500)
        with patch("httpx.get", return_value=self._html_resp("", status=403)), \
             patch("playwright.async_api.async_playwright",
                   return_value=_playwright(page)):
            jobs = await fetch_roles(["backend-engineer"])
        assert len(jobs) == 1  # browser path still ran

    def test_html_parsing_finds_posting_links_without_structured_data(self):
        from app.services.sources.wellfound import _jobs_from_html
        html = ('<a href="/jobs/1234-staff-engineer">Staff Engineer</a>'
                '<a href="/company/acme">Acme</a>')
        jobs = _jobs_from_html(html, "United States")
        assert len(jobs) == 1
        assert jobs[0]["title"] == "Staff Engineer"
        assert jobs[0]["url"] == "https://wellfound.com/jobs/1234-staff-engineer"

    def test_html_parsing_survives_malformed_structured_data(self):
        from app.services.sources.wellfound import _jobs_from_html
        html = '<script type="application/ld+json">{not json</script>'
        assert _jobs_from_html(html, "") == []

    @pytest.mark.asyncio
    async def test_a_nonexistent_role_page_is_named_as_such(self):
        from app.services.sources.wellfound import fetch_roles
        page = _page(evaluate=[self._ROW], body="x" * 500)
        page.goto = AsyncMock(return_value=MagicMock(status=404))
        with patch("playwright.async_api.async_playwright", return_value=_playwright(page)):
            jobs = await fetch_roles(["not-a-real-role"])
        assert jobs == []
        page.evaluate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_it_builds_jobs_from_the_extractor(self):
        from app.services.sources.wellfound import fetch
        page = _page(evaluate=[self._ROW], body="x" * 500)
        with patch("playwright.async_api.async_playwright", return_value=_playwright(page)):
            jobs = await fetch("Software Engineer", "United States")

        assert len(jobs) == 1
        assert jobs[0]["source"] == "wellfound"
        assert jobs[0]["company"] == "Acme"
        assert jobs[0]["url"].endswith("/jobs/12345-software-engineer")

    @pytest.mark.asyncio
    async def test_the_same_role_is_not_reloaded_for_each_location(self):
        """A role URL has no location in it, so loading it twice is pure waste."""
        from app.services.sources.wellfound import fetch
        page = _page(evaluate=[self._ROW], body="x" * 500)
        with patch("playwright.async_api.async_playwright", return_value=_playwright(page)):
            await fetch("Software Engineer", "United States")
            await fetch("Software Engineer", "Canada")
        assert page.goto.await_count == 1

    @pytest.mark.asyncio
    async def test_a_different_role_does_load_its_own_page(self):
        from app.services.sources.wellfound import fetch
        page = _page(evaluate=[self._ROW], body="x" * 500)
        with patch("playwright.async_api.async_playwright", return_value=_playwright(page)):
            await fetch("Software Engineer", "United States")
            await fetch("Data Engineer", "United States")
        assert page.goto.await_count == 2

    @pytest.mark.asyncio
    async def test_a_page_that_renders_no_text_is_reported_as_a_block(self):
        """body_chars=0 is not a selector problem — say so plainly."""
        from app.services.sources.wellfound import fetch
        page = _page(title="wellfound.com", body="")
        page.wait_for_function = AsyncMock(side_effect=RuntimeError("timeout"))
        with patch("playwright.async_api.async_playwright", return_value=_playwright(page)):
            jobs = await fetch("Software Engineer", "United States")

        assert jobs == []
        # It gave up before extracting, since there is nothing to extract.
        page.evaluate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_it_falls_back_to_embedded_app_state(self):
        from app.services.sources.wellfound import fetch
        blob = json.dumps({"jobs": [{
            "id": 7, "title": "Platform Engineer",
            "url": "https://wellfound.com/jobs/7-platform",
            "startupName": "Beta", "locationStr": "Remote",
        }]})
        page = _page(evaluate=[], body="x" * 500)
        page.evaluate = AsyncMock(side_effect=[[], blob])
        with patch("playwright.async_api.async_playwright", return_value=_playwright(page)):
            jobs = await fetch("Platform Engineer", "United States")

        assert len(jobs) == 1
        assert jobs[0]["title"] == "Platform Engineer"

    @pytest.mark.asyncio
    async def test_the_search_location_fills_in_when_the_page_omits_one(self):
        from app.services.sources.wellfound import fetch
        row = {**self._ROW, "location": ""}
        page = _page(evaluate=[row], body="x" * 500)
        with patch("playwright.async_api.async_playwright", return_value=_playwright(page)):
            jobs = await fetch("Software Engineer", "United States")
        assert jobs[0]["location"] == "United States"

    def test_the_extractor_covers_structured_data_and_links(self):
        from app.services.sources.wellfound import _EXTRACT_JS
        assert "application/ld+json" in _EXTRACT_JS
        assert '/jobs/' in _EXTRACT_JS

    @pytest.mark.asyncio
    async def test_a_new_cycle_refetches_rather_than_serving_stale_results(self):
        """
        The cache exists to avoid reloading one role page per location within a
        cycle. If it outlived the cycle, re-triggering a fetch after changing an
        adapter would replay the old results and look like nothing changed.
        """
        from app.services.job_fetcher import _reset_source_caches
        from app.services.sources.wellfound import fetch

        page = _page(evaluate=[self._ROW], body="x" * 500)
        with patch("playwright.async_api.async_playwright", return_value=_playwright(page)):
            await fetch("Software Engineer", "United States")
            _reset_source_caches()          # what the start of a cycle does
            await fetch("Software Engineer", "United States")

        assert page.goto.await_count == 2

    def test_the_cycle_reset_clears_every_cached_adapter(self):
        from app.services.job_fetcher import _reset_source_caches
        from app.services.sources import arbeitnow, wellfound

        wellfound._cache["software-engineer"] = (9e9, [{"title": "stale"}])
        arbeitnow._cache["items"] = [{"title": "stale"}]
        _reset_source_caches()
        assert wellfound._cache == {}
        assert arbeitnow._cache["items"] == []
