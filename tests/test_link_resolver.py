import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from app.models.job import Job, JobStatus
from app.services.link_resolver import (
    is_aggregator,
    is_interstitial,
    is_tracker,
    resolve_jobs,
    retarget_tracker_links,
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


class TestTrackerChains:
    """
    Click trackers are not job boards, they are redirect middlemen — and most
    "resolved" Adzuna links stopped on one. The resolver followed HTTP
    redirects only; appcast bounces with JavaScript, so the tracker's own page
    was the last thing it saw and it recorded that as the destination.
    """

    def _job(self, url, source="adzuna"):
        return {"source": source, "url": url, "title": "SWE", "company": "Acme"}

    def test_trackers_are_recognised_as_interstitials(self):
        assert is_interstitial("https://click.appcast.io/track/abc?cs=1")
        assert is_interstitial("https://jsv3.recruitics.com/redirect?rx=1")
        assert is_interstitial("https://click.jobvite.com/e/x?u=1")

    def test_trackers_are_never_a_valid_destination(self):
        assert is_aggregator("https://click.appcast.io/track/abc")
        assert is_tracker("https://click.appcast.io/track/abc")

    def test_the_jobvite_ats_itself_is_still_a_valid_destination(self):
        # Only the click-tracker subdomain is a middleman; jobs.jobvite.com is
        # a real ATS page we want to keep.
        assert not is_aggregator("https://jobs.jobvite.com/acme/job/oX1")
        assert not is_tracker("https://jobs.jobvite.com/acme/job/oX1")

    def test_a_javascript_bounce_is_followed(self):
        job = self._job("https://www.adzuna.com/land/ad/10")
        responses = {
            "https://www.adzuna.com/land/ad/10": _resp(
                "https://click.appcast.io/track/x",
                '<script>window.location.replace("https://acme.wd5.myworkdayjobs.com/j/1");</script>',
            ),
            "https://acme.wd5.myworkdayjobs.com/j/1": _resp(
                "https://acme.wd5.myworkdayjobs.com/j/1", "<html>jd</html>"
            ),
        }
        with patch("app.services.link_resolver.httpx.Client",
                   return_value=_fake_client(responses)):
            resolve_jobs([job], max_links=10)
        assert job["apply_url"] == "https://acme.wd5.myworkdayjobs.com/j/1"

    def test_a_multi_hop_chain_is_followed_to_the_end(self):
        """Adzuna → appcast → recruitics → the employer's ATS."""
        job = self._job("https://www.adzuna.com/land/ad/11")
        responses = {
            "https://www.adzuna.com/land/ad/11": _resp(
                "https://click.appcast.io/track/y",
                '<meta http-equiv="refresh" content="0;url=https://jsv3.recruitics.com/redirect?rx=2">',
            ),
            "https://jsv3.recruitics.com/redirect?rx=2": _resp(
                "https://jsv3.recruitics.com/redirect?rx=2",
                '<script>location.href="https://boards.greenhouse.io/acme/jobs/5"</script>',
            ),
            "https://boards.greenhouse.io/acme/jobs/5": _resp(
                "https://boards.greenhouse.io/acme/jobs/5", "<html>jd</html>"
            ),
        }
        with patch("app.services.link_resolver.httpx.Client",
                   return_value=_fake_client(responses)):
            stats = resolve_jobs([job], max_links=10)
        assert job["apply_url"] == "https://boards.greenhouse.io/acme/jobs/5"
        assert stats.stopped_at_aggregator == 0

    def test_a_tracker_is_never_stored_as_the_apply_url(self):
        job = self._job("https://www.adzuna.com/land/ad/12")
        responses = {
            "https://www.adzuna.com/land/ad/12": _resp(
                "https://click.appcast.io/track/z", "<html>no way out</html>"
            ),
        }
        with patch("app.services.link_resolver.httpx.Client",
                   return_value=_fake_client(responses)):
            stats = resolve_jobs([job], max_links=10)
        assert "apply_url" not in job
        assert stats.stopped_at_aggregator == 1

    def test_a_destination_spelled_out_in_the_query_is_used(self):
        """A tracker that refuses the request still names where it was going."""
        import httpx
        url = ("https://click.appcast.io/track/x"
               "?destination=https%3A%2F%2Fjobs.lever.co%2Facme%2F7")
        job = self._job(url)
        responses = {url: httpx.ConnectError("403")}
        with patch("app.services.link_resolver.httpx.Client",
                   return_value=_fake_client(responses)):
            stats = resolve_jobs([job], max_links=10)
        assert job["apply_url"] == "https://jobs.lever.co/acme/7"
        assert stats.failed == 0

    def test_a_redirect_loop_terminates(self):
        job = self._job("https://www.adzuna.com/land/ad/13")
        responses = {
            "https://www.adzuna.com/land/ad/13": _resp(
                "https://click.appcast.io/a",
                '<script>location.href="https://jsv3.recruitics.com/b"</script>',
            ),
            "https://jsv3.recruitics.com/b": _resp(
                "https://jsv3.recruitics.com/b",
                '<script>location.href="https://click.appcast.io/a"</script>',
            ),
            "https://click.appcast.io/a": _resp(
                "https://click.appcast.io/a",
                '<script>location.href="https://jsv3.recruitics.com/b"</script>',
            ),
        }
        with patch("app.services.link_resolver.httpx.Client",
                   return_value=_fake_client(responses)):
            stats = resolve_jobs([job], max_links=10)
        assert "apply_url" not in job
        assert stats.stopped_at_aggregator == 1


def _stored_job(**kwargs) -> Job:
    defaults = dict(
        source="adzuna",
        source_urls=["https://www.adzuna.com/land/ad/1"],
        title="Backend Engineer",
        company="Acme",
        location="Remote",
        url="https://www.adzuna.com/land/ad/1",
        status=JobStatus.new,
        fetched_at=datetime.now(timezone.utc),
        dedupe_hash=uuid.uuid4().hex,
    )
    defaults.update(kwargs)
    return Job(**defaults)


class TestRetargetStoredTrackerLinks:
    """
    The jobs already carrying a tracker as their "real apply URL". Resumed from
    the tracker rather than restarted, because that is the deepest point the
    old resolver reached.
    """

    def test_a_tracker_that_now_resolves_is_repaired(self, db):
        tracker = "https://click.appcast.io/track/aa"
        job = _stored_job(apply_url=tracker)
        db.add(job)
        db.commit()

        responses = {tracker: _resp("https://boards.greenhouse.io/acme/jobs/9")}
        with patch("app.services.link_resolver.httpx.Client",
                   return_value=_fake_client(responses)):
            counts = retarget_tracker_links(db)

        db.refresh(job)
        assert counts["repaired"] == 1
        assert job.apply_url == "https://boards.greenhouse.io/acme/jobs/9"

    def test_a_tracker_that_leads_nowhere_is_cleared(self, db):
        """
        Cleared rather than left in place: it is not something the user can
        apply through, and NULL is what puts the job back in front of browser
        resolution.
        """
        tracker = "https://click.appcast.io/track/bb"
        job = _stored_job(apply_url=tracker,
                          url="https://www.adzuna.com/land/ad/2",
                          source_urls=["https://www.adzuna.com/land/ad/2"])
        db.add(job)
        db.commit()

        responses = {tracker: _resp(tracker, "<html>dead end</html>")}
        with patch("app.services.link_resolver.httpx.Client",
                   return_value=_fake_client(responses)):
            counts = retarget_tracker_links(db)

        db.refresh(job)
        assert counts["cleared"] == 1
        assert job.apply_url is None

    def test_real_apply_urls_are_left_alone(self, db):
        job = _stored_job(apply_url="https://boards.greenhouse.io/acme/jobs/1",
                          url="https://www.adzuna.com/land/ad/3",
                          source_urls=["https://www.adzuna.com/land/ad/3"])
        db.add(job)
        db.commit()

        with patch("app.services.link_resolver.httpx.Client") as client:
            counts = retarget_tracker_links(db)
        client.assert_not_called()

        db.refresh(job)
        assert counts["examined"] == 0
        assert job.apply_url == "https://boards.greenhouse.io/acme/jobs/1"

    def test_every_job_sharing_one_tracker_is_repaired(self, db):
        tracker = "https://click.appcast.io/track/cc"
        for i in range(3):
            db.add(_stored_job(
                apply_url=tracker,
                url=f"https://www.adzuna.com/land/ad/1{i}",
                source_urls=[f"https://www.adzuna.com/land/ad/1{i}"],
            ))
        db.commit()

        responses = {tracker: _resp("https://jobs.lever.co/acme/3")}
        with patch("app.services.link_resolver.httpx.Client",
                   return_value=_fake_client(responses)):
            counts = retarget_tracker_links(db)

        assert counts["examined"] == 1  # one distinct URL followed
        assert counts["repaired"] == 3  # three jobs updated


class TestHostPoliteness:
    def test_requests_to_one_host_are_capped_and_spaced(self):
        """
        The per-cycle budget is high now, so the only thing standing between a
        backlog and hammering somebody is this.
        """
        import threading
        from app.services.link_resolver import _HostLimiter

        limiter = _HostLimiter(max_concurrent=2, min_interval=0.0)
        in_flight = 0
        peak = 0
        guard = threading.Lock()

        def _work():
            nonlocal in_flight, peak
            with limiter.slot("https://www.adzuna.com/land/ad/1"):
                with guard:
                    in_flight += 1
                    peak = max(peak, in_flight)
                import time as _t
                _t.sleep(0.02)
                with guard:
                    in_flight -= 1

        threads = [threading.Thread(target=_work) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert peak <= 2

    def test_different_hosts_do_not_wait_on_each_other(self):
        import time
        from app.services.link_resolver import _HostLimiter

        limiter = _HostLimiter(max_concurrent=1, min_interval=0.2)
        started = time.monotonic()
        with limiter.slot("https://a.example.com/1"):
            pass
        with limiter.slot("https://b.example.com/1"):
            pass
        # Two different hosts, so neither paid the other's gap.
        assert time.monotonic() - started < 0.2
