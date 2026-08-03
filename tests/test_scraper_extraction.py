"""
Tests for the two scraper fixes the page diagnostics pointed at.

Dice loaded a real page (title 'Search Jobs | Dice.com', 2000+ chars of body)
but the <dhi-job-card> element it keyed on no longer exists — a markup change,
so extraction was rewritten to be markup-independent. Wellfound rendered a body
of exactly zero characters, which is not a selector problem at all.
"""

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


class TestWellfoundEmptyRender:
    @pytest.mark.asyncio
    async def test_a_page_that_renders_no_text_is_reported_as_such(self):
        """body_chars=0 is not a selector problem — say so plainly."""
        from app.services.sources.wellfound import fetch
        page = _page(title="wellfound.com", body="")
        page.wait_for_function = AsyncMock(side_effect=RuntimeError("timeout"))
        with patch("playwright.async_api.async_playwright", return_value=_playwright(page)):
            jobs = await fetch("Software Engineer", "United States")

        assert jobs == []
        # It gave up before trying selectors, since there is nothing to select.
        page.evaluate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_page_that_does_render_proceeds_to_extraction(self):
        from app.services.sources.wellfound import fetch
        page = _page(title="Startup Jobs", body="x" * 500)
        page.query_selector_all = AsyncMock(return_value=[])
        with patch("playwright.async_api.async_playwright", return_value=_playwright(page)):
            await fetch("Software Engineer", "United States")
        page.evaluate.assert_awaited()
