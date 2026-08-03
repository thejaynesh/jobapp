from unittest.mock import MagicMock, patch

from app.services.link_resolver import (
    is_aggregator,
    is_interstitial,
    resolve_jobs,
)


def _resp(url: str, text: str = "", content_type: str = "text/html") -> MagicMock:
    resp = MagicMock()
    resp.url = url
    resp.text = text
    resp.headers = {"content-type": content_type}
    return resp


def _fake_client(responses: dict):
    """Patch target for httpx.Client: maps requested URL → response or Exception."""
    client = MagicMock()

    def _get(url, **kwargs):
        result = responses.get(url)
        if result is None:
            return _resp(url)
        if isinstance(result, Exception):
            raise result
        return result

    client.get.side_effect = _get
    ctx = MagicMock()
    ctx.__enter__.return_value = client
    ctx.__exit__.return_value = False
    return ctx


class TestInterstitialDetection:
    def test_recognises_aggregator_redirect_pages(self):
        assert is_interstitial("https://www.adzuna.com/land/ad/5123456789?v=ABC")
        assert is_interstitial("https://uk.adzuna.co.uk/land/ad/999")
        assert is_interstitial("https://jooble.org/away/12345")
        assert is_interstitial("https://www.careerjet.com/jobad/us1234")
        assert is_interstitial("https://www.indeed.com/rc/clk?jk=abc")

    def test_leaves_real_posting_urls_alone(self):
        assert not is_interstitial("https://boards.greenhouse.io/stripe/jobs/123")
        assert not is_interstitial("https://careers.acme.com/job/42")
        assert not is_interstitial("")

    def test_identifies_aggregator_hosts(self):
        assert is_aggregator("https://www.indeed.com/viewjob?jk=1")
        assert not is_aggregator("https://boards.greenhouse.io/stripe/jobs/1")


class TestResolveJobs:
    def _job(self, url, source="adzuna"):
        return {"source": source, "url": url, "title": "SWE", "company": "Acme"}

    def test_sets_apply_url_from_redirect_chain(self):
        job = self._job("https://www.adzuna.com/land/ad/1")
        responses = {
            "https://www.adzuna.com/land/ad/1": _resp(
                "https://boards.greenhouse.io/acme/jobs/77", "<html>jd</html>"
            )
        }
        with patch("app.services.link_resolver.httpx.Client",
                   return_value=_fake_client(responses)):
            stats = resolve_jobs([job], max_links=10)

        assert job["apply_url"] == "https://boards.greenhouse.io/acme/jobs/77"
        assert stats.resolved == 1
        assert stats.attempted == 1

    def test_landing_on_another_aggregator_is_not_an_apply_link(self):
        job = self._job("https://www.adzuna.com/land/ad/2")
        responses = {
            "https://www.adzuna.com/land/ad/2": _resp(
                "https://www.indeed.com/viewjob?jk=9",
                '<a href="https://jobs.lever.co/acme/1">apply</a>',
            )
        }
        with patch("app.services.link_resolver.httpx.Client",
                   return_value=_fake_client(responses)):
            stats = resolve_jobs([job], max_links=10)

        assert "apply_url" not in job
        # The page is still kept — the ATS link inside it is the real prize.
        assert "jobs.lever.co/acme" in stats.landing_html[job["url"]]

    def test_follows_meta_refresh_when_there_is_no_3xx(self):
        job = self._job("https://jooble.org/away/5")
        responses = {
            "https://jooble.org/away/5": _resp(
                "https://jooble.org/away/5",
                '<meta http-equiv="refresh" content="0;url=https://jobs.ashbyhq.com/acme/1">',
            ),
            "https://jobs.ashbyhq.com/acme/1": _resp(
                "https://jobs.ashbyhq.com/acme/1", "<html>jd</html>"
            ),
        }
        with patch("app.services.link_resolver.httpx.Client",
                   return_value=_fake_client(responses)):
            resolve_jobs([job], max_links=10)

        assert job["apply_url"] == "https://jobs.ashbyhq.com/acme/1"

    def test_non_interstitial_jobs_are_left_untouched(self):
        job = self._job("https://boards.greenhouse.io/acme/jobs/1", source="greenhouse")
        with patch("app.services.link_resolver.httpx.Client") as client:
            stats = resolve_jobs([job], max_links=10)
        client.assert_not_called()
        assert stats.attempted == 0
        assert "apply_url" not in job

    def test_shared_url_is_resolved_once_for_every_job(self):
        url = "https://www.adzuna.com/land/ad/3"
        jobs = [self._job(url), self._job(url)]
        responses = {url: _resp("https://jobs.lever.co/acme/9")}
        with patch("app.services.link_resolver.httpx.Client",
                   return_value=_fake_client(responses)) as client_factory:
            stats = resolve_jobs(jobs, max_links=10)

        inner = client_factory.return_value.__enter__.return_value
        assert inner.get.call_count == 1
        assert stats.attempted == 1
        assert all(j["apply_url"] == "https://jobs.lever.co/acme/9" for j in jobs)

    def test_budget_defers_the_overflow(self):
        jobs = [self._job(f"https://www.adzuna.com/land/ad/{i}") for i in range(5)]
        with patch("app.services.link_resolver.httpx.Client",
                   return_value=_fake_client({})):
            stats = resolve_jobs(jobs, max_links=2)
        assert stats.attempted == 2
        assert stats.skipped_budget == 3

    def test_network_failure_is_counted_not_raised(self):
        import httpx
        job = self._job("https://www.adzuna.com/land/ad/4")
        responses = {job["url"]: httpx.ConnectError("boom")}
        with patch("app.services.link_resolver.httpx.Client",
                   return_value=_fake_client(responses)):
            stats = resolve_jobs([job], max_links=10)
        assert stats.failed == 1
        assert "apply_url" not in job
