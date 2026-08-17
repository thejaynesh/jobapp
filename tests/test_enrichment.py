"""
Going back for the description the source did not send.

The rule under test everywhere: a job is only rewritten when the new text is
meaningfully fuller than what it had, and a job that was rejected for having no
description gets judged again once it has one. That second half is the point of
the feature — ~25,000 jobs were filtered for thin data rather than for being
bad jobs, and nothing in the pipeline ever looked at them again.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app.models.job import Job, JobStatus
from app.services import enrichment


def _resp(payload=None, text="", content_type="application/json", status=200):
    resp = MagicMock()
    resp.json.return_value = payload if payload is not None else {}
    resp.text = text
    resp.status_code = status
    resp.headers = {"content-type": content_type}
    resp.raise_for_status = MagicMock()
    return resp


def _client(responses: dict):
    """A stand-in httpx.Client mapping URL → response."""
    client = MagicMock()

    def _get(url, **kwargs):
        found = responses.get(url)
        if found is None:
            raise AssertionError(f"unexpected request to {url}")
        if isinstance(found, Exception):
            raise found
        return found

    client.get.side_effect = _get
    return client


def _job(**kwargs) -> Job:
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


LONG = "We need a backend engineer with deep Python and Go experience. " * 30


class TestAtsShortcuts:
    """
    The cheapest method, and the one that has to keep working: when the URL
    names the ATS, the clean description is one JSON request away — no
    scraping, no markup, no model call.
    """

    def test_greenhouse_content_is_unescaped_and_stripped(self):
        url = "https://boards.greenhouse.io/acme/jobs/4567"
        client = _client({
            "https://boards-api.greenhouse.io/v1/boards/acme/jobs/4567": _resp({
                "content": "&lt;p&gt;Build things&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;&lt;/ul&gt;",
                "first_published": "2026-08-01T00:00:00Z",
                "location": {"name": "Remote"},
            }),
        })
        found = enrichment.enrich_one(client, url)
        assert found.method == "ats_api"
        assert found.description == "Build things\n\n- Python"
        assert found.details["location"] == "Remote"

    def test_lever_keeps_the_requirement_lists(self):
        """
        descriptionPlain is only the opening section. Dropping `lists` is how a
        Lever job ends up looking like two paragraphs of marketing.
        """
        url = "https://jobs.lever.co/acme/" + "0" * 8 + "-1111-2222-3333-" + "4" * 12
        posting_id = url.rsplit("/", 1)[1]
        client = _client({
            f"https://api.lever.co/v0/postings/acme/{posting_id}": _resp({
                "descriptionPlain": "About the role.",
                "lists": [{"text": "Requirements",
                           "content": "<ul><li>5 years Python</li></ul>"}],
                "additionalPlain": "We offer equity.",
            }),
        })
        found = enrichment.enrich_one(client, url)
        assert "5 years Python" in found.description
        assert "Requirements" in found.description
        assert "We offer equity." in found.description

    def test_workday_reads_the_cxs_endpoint_the_page_itself_calls(self):
        url = "https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/NYC/Engineer_R-1"
        client = _client({
            "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Careers/job/NYC/Engineer_R-1":
                _resp({"jobPostingInfo": {
                    "jobDescription": "<p>Workday role</p>",
                    "startDate": "2026-08-01",
                }}),
        })
        found = enrichment.enrich_one(client, url)
        assert found.description == "Workday role"
        assert found.method == "ats_api"

    def test_a_failing_ats_api_falls_through_to_the_page(self):
        import httpx
        url = "https://boards.greenhouse.io/acme/jobs/999"
        client = _client({
            "https://boards-api.greenhouse.io/v1/boards/acme/jobs/999":
                httpx.HTTPError("500"),
            url: _resp(
                text='<script type="application/ld+json">'
                     '{"@type":"JobPosting","description":"<p>' + LONG + '</p>"}'
                     '</script>',
                content_type="text/html",
            ),
        })
        found = enrichment.enrich_one(client, url)
        assert found.method == "json_ld"
        assert "backend engineer" in found.description

    def test_non_ats_urls_do_not_try_an_api(self):
        assert not enrichment.looks_like_ats("https://careers.acme.com/job/1")
        assert enrichment.looks_like_ats("https://jobs.ashbyhq.com/acme/"
                                         + "a" * 8 + "-bbbb-cccc-dddd-" + "e" * 12)


class TestJsonLd:
    def test_a_job_posting_block_is_read(self):
        html = (
            '<html><script type="application/ld+json">'
            '{"@type": "JobPosting", "description": "<p>Real description here</p>",'
            ' "datePosted": "2026-08-02", "employmentType": "FULL_TIME",'
            ' "baseSalary": {"currency": "USD", "value": '
            '{"minValue": 120000, "maxValue": 160000}}}'
            "</script></html>"
        )
        found = enrichment.json_ld_extraction(html)
        assert found.description == "Real description here"
        assert found.posted_at == "2026-08-02"
        assert found.details["salary_min"] == 120000
        assert found.details["salary_max"] == 160000
        assert found.details["salary_currency"] == "USD"
        assert found.details["employment_type"] == "FULL_TIME"

    def test_a_posting_nested_in_a_graph_is_found(self):
        html = (
            '<script type="application/ld+json">'
            '{"@graph": [{"@type": "Organization"},'
            ' {"@type": "JobPosting", "description": "Nested but found"}]}'
            "</script>"
        )
        assert enrichment.json_ld_extraction(html).description == "Nested but found"

    def test_a_page_with_no_posting_block_yields_nothing(self):
        html = '<script type="application/ld+json">{"@type": "WebSite"}</script>'
        assert not enrichment.json_ld_extraction(html)

    def test_broken_json_does_not_raise(self):
        html = '<script type="application/ld+json">{not json at all</script>'
        assert not enrichment.json_ld_extraction(html)


class TestLlmExtraction:
    def _reply(self, text):
        return patch("app.llm.providers.generation_chat", return_value=text)

    def test_a_described_posting_is_extracted(self):
        with self._reply(
            '{"is_job_posting": true, "description": "' + LONG.strip() + '",'
            ' "employment_type": "full_time"}'
        ):
            found = enrichment.llm_extraction("<html><body>" + LONG + "</body></html>")
        assert found.method == "llm"
        assert "backend engineer" in found.description
        assert found.details["employment_type"] == "full_time"

    def test_a_page_that_is_not_a_posting_yields_nothing(self):
        """A search results page or a login wall must not become a description."""
        with self._reply('{"is_job_posting": false}'):
            assert not enrichment.llm_extraction("<html>" + LONG + "</html>")

    def test_a_reply_wrapped_in_prose_is_still_parsed(self):
        with self._reply(
            'Here you go:\n```json\n{"is_job_posting": true, '
            '"description": "' + LONG.strip() + '"}\n```'
        ):
            assert enrichment.llm_extraction("<html>" + LONG + "</html>")

    def test_a_provider_failure_is_swallowed(self):
        with patch("app.llm.providers.generation_chat",
                   side_effect=RuntimeError("all providers down")):
            assert not enrichment.llm_extraction("<html>" + LONG + "</html>")

    def test_a_page_too_short_to_be_a_posting_never_reaches_the_model(self):
        with patch("app.llm.providers.generation_chat") as chat:
            assert not enrichment.llm_extraction("<html>Tiny</html>")
        chat.assert_not_called()


class TestApplyExtraction:
    def test_a_meaningfully_fuller_description_is_stored_and_stamped(self, db):
        job = _job(description="Adzuna stub, 500 chars, cut off mid-")
        db.add(job)
        db.commit()

        outcome = enrichment.apply_extraction(
            db, job, enrichment.Extraction(description=LONG, method="ats_api")
        )
        assert outcome["improved"] is True
        assert job.description == LONG
        # Unlike cleaning, this IS new information — the docs nudge should fire.
        assert job.description_updated_at is not None

    def test_a_marginal_gain_is_not_worth_a_write(self, db):
        job = _job(description="x" * 1000)
        db.add(job)
        db.commit()

        outcome = enrichment.apply_extraction(
            db, job, enrichment.Extraction(description="x" * 1050, method="llm")
        )
        assert outcome["improved"] is False
        assert job.description == "x" * 1000

    def test_a_shorter_result_never_replaces_a_longer_one(self, db):
        job = _job(description=LONG)
        db.add(job)
        db.commit()

        enrichment.apply_extraction(
            db, job, enrichment.Extraction(description="Short.", method="llm")
        )
        assert job.description == LONG

    def test_a_job_filtered_for_no_description_goes_back_to_new(self, db):
        """
        The point of the whole feature: it was never rejected on its merits.
        """
        job = _job(
            description=None,
            status=JobStatus.filtered_out,
            filter_reason="no_description",
            filter_detail="The source returned no description.",
        )
        db.add(job)
        db.commit()

        outcome = enrichment.apply_extraction(
            db, job, enrichment.Extraction(description=LONG, method="ats_api")
        )
        assert outcome["requeued"] is True
        assert job.status == JobStatus.new
        assert job.filter_reason is None
        assert job.filter_detail is None

    def test_a_job_filtered_for_a_real_reason_stays_filtered(self, db):
        # "Title doesn't match your roles" is a verdict on data we had. A
        # fuller description does not change it, and un-filtering here would
        # put the job back in front of the user for no reason.
        job = _job(
            description="short",
            status=JobStatus.filtered_out,
            filter_reason="title_mismatch",
        )
        db.add(job)
        db.commit()

        outcome = enrichment.apply_extraction(
            db, job, enrichment.Extraction(description=LONG, method="ats_api")
        )
        assert outcome["improved"] is True
        assert outcome["requeued"] is False
        assert job.status == JobStatus.filtered_out

    def test_a_missing_posted_at_is_filled_but_never_overwritten(self, db):
        known = datetime(2026, 1, 1, tzinfo=timezone.utc)
        undated = _job(description="short")
        dated = _job(description="short", posted_at=known,
                     url="https://www.adzuna.com/land/ad/2",
                     source_urls=["https://www.adzuna.com/land/ad/2"])
        db.add_all([undated, dated])
        db.commit()

        found = enrichment.Extraction(
            description=LONG, method="ats_api", posted_at="2026-08-01T00:00:00Z"
        )
        enrichment.apply_extraction(db, undated, found)
        enrichment.apply_extraction(db, dated, found)

        assert undated.posted_at.year == 2026 and undated.posted_at.month == 8
        assert dated.posted_at == known


class TestTargetSelection:
    def test_thin_and_missing_descriptions_are_targets(self, db):
        thin = _job(description="tiny", url="https://x/1", source_urls=["https://x/1"])
        missing = _job(description=None, url="https://x/2", source_urls=["https://x/2"])
        full = _job(description=LONG, url="https://x/3", source_urls=["https://x/3"])
        db.add_all([thin, missing, full])
        db.commit()

        ids = {j.id for j in enrichment.select_targets(db, {}, limit=50)}
        assert thin.id in ids and missing.id in ids
        assert full.id not in ids

    def test_jobs_rejected_for_thin_data_are_included(self, db):
        rescuable = _job(
            description=None, status=JobStatus.filtered_out,
            filter_reason="few_skills",
            url="https://x/4", source_urls=["https://x/4"],
        )
        db.add(rescuable)
        db.commit()
        assert rescuable.id in {j.id for j in enrichment.select_targets(db, {}, limit=50)}

    def test_jobs_rejected_on_their_merits_are_not(self, db):
        judged = _job(
            description="tiny", status=JobStatus.filtered_out,
            filter_reason="title_mismatch",
            url="https://x/5", source_urls=["https://x/5"],
        )
        db.add(judged)
        db.commit()
        assert judged.id not in {j.id for j in enrichment.select_targets(db, {}, limit=50)}

    def test_closed_postings_are_never_targets(self, db):
        closed = _job(description=None, closed_at=datetime.now(timezone.utc),
                      url="https://x/6", source_urls=["https://x/6"])
        db.add(closed)
        db.commit()
        assert closed.id not in {j.id for j in enrichment.select_targets(db, {}, limit=50)}

    def test_title_passing_jobs_are_worked_first(self, db):
        """
        The backlog is bigger than any pass, so ordering matters more than the
        budget: a job whose title the matcher would reject gains nothing from a
        fuller description.
        """
        now = datetime.now(timezone.utc)
        # The irrelevant one is NEWER, so only the title gate can reorder them.
        wanted = _job(title="Backend Engineer", description=None,
                      fetched_at=now - timedelta(hours=1),
                      url="https://x/7", source_urls=["https://x/7"])
        unwanted = _job(title="Dental Hygienist", description=None,
                        fetched_at=now,
                        url="https://x/8", source_urls=["https://x/8"])
        db.add_all([wanted, unwanted])
        db.commit()

        targets = enrichment.select_targets(
            db, {"target_roles": ["Backend Engineer"]}, limit=50
        )
        assert targets[0].id == wanted.id


class TestEnrichJobs:
    def test_a_walled_host_is_handed_to_the_browser_not_requested(self, db):
        """
        LinkedIn answers this server with a challenge and a real browser with
        the posting. Spending a request and a timeout on it first is pure loss.
        """
        job = _job(source="linkedin", description=None,
                   url="https://www.linkedin.com/jobs/view/123/",
                   source_urls=["https://www.linkedin.com/jobs/view/123/"])
        db.add(job)
        db.commit()

        with patch("app.services.enrichment.httpx.Client") as client:
            stats = enrichment.enrich_jobs(db, [job])
        client.assert_not_called()
        assert stats.queued_browser == 1
        assert stats.attempted == 0

    def test_a_page_already_in_hand_is_not_downloaded_again(self, db):
        """
        Link resolution just fetched this page and threw it away after mining
        it for board slugs. The description was in it the whole time.
        """
        job = _job(description=None)
        db.add(job)
        db.commit()

        html = ('<script type="application/ld+json">'
                '{"@type":"JobPosting","description":"' + LONG.strip() + '"}</script>')
        with patch("app.services.enrichment.httpx.Client") as factory:
            inner = MagicMock()
            factory.return_value.__enter__.return_value = inner
            stats = enrichment.enrich_jobs(db, [job], landing_html={job.url: html})

        inner.get.assert_not_called()
        assert stats.enriched == 1
        assert stats.via == {"landing_html": 1}

    def test_a_failure_is_counted_against_its_host(self, db):
        import httpx
        job = _job(description=None, url="https://careers.acme.com/job/1",
                   source_urls=["https://careers.acme.com/job/1"])
        db.add(job)
        db.commit()

        with patch("app.services.enrichment.httpx.Client") as factory:
            inner = MagicMock()
            inner.get.side_effect = httpx.ConnectError("refused")
            factory.return_value.__enter__.return_value = inner
            stats = enrichment.enrich_jobs(db, [job])

        assert stats.failed == 1
        assert stats.failures_by_host == {"careers.acme.com": 1}

    def test_the_apply_url_is_preferred_over_the_listing_url(self, db):
        # The listing URL is an Adzuna redirect; the apply URL is the
        # employer's own page, and the ATS shortcut only works on that one.
        job = _job(description=None,
                   apply_url="https://boards.greenhouse.io/acme/jobs/1")
        assert enrichment._target_url(job) == "https://boards.greenhouse.io/acme/jobs/1"


class TestRunRecording:
    def test_a_pass_writes_a_row(self, db):
        from app.models.enrichment_run import EnrichmentRun
        from app.models.profile import Profile

        db.add(Profile(data={"target_roles": ["Backend Engineer"]}))
        job = _job(description=None, url="https://careers.acme.com/job/2",
                   source_urls=["https://careers.acme.com/job/2"])
        db.add(job)
        db.commit()

        html = ('<script type="application/ld+json">'
                '{"@type":"JobPosting","description":"' + LONG.strip() + '"}</script>')
        with patch("app.services.enrichment.httpx.Client") as factory:
            inner = MagicMock()
            inner.get.return_value = _resp(text=html, content_type="text/html")
            factory.return_value.__enter__.return_value = inner
            result = enrichment.run(db, limit=10)

        assert result["enriched"] == 1
        run = db.query(EnrichmentRun).order_by(EnrichmentRun.started_at.desc()).first()
        assert run is not None
        assert run.enriched == 1
        assert run.via_json_ld == 1
        assert run.chars_gained > 0
        assert run.status == "ok"

    def test_a_pass_that_finds_nothing_still_records_itself(self, db):
        from app.models.enrichment_run import EnrichmentRun

        result = enrichment.run(db, limit=10)
        assert result["attempted"] == 0
        assert db.query(EnrichmentRun).count() == 1

    def test_the_backlog_is_counted_for_the_panel(self, db):
        from app.services import enrichment_history

        db.add(_job(description=None, url="https://x/9", source_urls=["https://x/9"]))
        db.add(_job(description=None, status=JobStatus.filtered_out,
                    filter_reason="no_description",
                    url="https://x/10", source_urls=["https://x/10"]))
        db.commit()

        counts = enrichment_history.backlog(db)
        assert counts["thin"] >= 2
        assert counts["rescuable"] >= 1


class TestBrowserResults:
    def test_a_browser_fetched_page_enriches_the_job_it_was_queued_for(self, db):
        from app.services import browser_tasks
        from app.services.agent_work import ingest

        job = _job(source="linkedin", description=None,
                   url="https://www.linkedin.com/jobs/view/555/",
                   source_urls=["https://www.linkedin.com/jobs/view/555/"],
                   status=JobStatus.filtered_out, filter_reason="no_description")
        db.add(job)
        db.commit()

        html = ('<script type="application/ld+json">'
                '{"@type":"JobPosting","description":"' + LONG.strip() + '"}</script>')
        task = browser_tasks.enqueue(
            db, "resolve_link",
            {"url": job.url, "purpose": "enrich", "job_id": str(job.id)},
        )
        task.status = "done"
        task.result = {"final_url": job.url, "html": html}
        db.commit()

        ingest(db, task)
        db.refresh(job)

        assert len(job.description) > 1000
        assert job.status == JobStatus.new
        assert task.result["ingest"]["enriched"] is True

    def test_a_browser_result_for_a_vanished_job_is_noted_not_raised(self, db):
        from app.services import browser_tasks
        from app.services.agent_work import ingest

        task = browser_tasks.enqueue(
            db, "resolve_link",
            {"url": "https://x/1", "purpose": "enrich", "job_id": str(uuid.uuid4())},
        )
        task.status = "done"
        task.result = {"final_url": "https://x/1", "html": "<html></html>"}
        db.commit()

        ingest(db, task)
        assert "error" in task.result["ingest"]

    def test_plain_link_resolution_still_works(self, db):
        """The enrichment branch must not swallow the original purpose."""
        from app.services import browser_tasks
        from app.services.agent_work import ingest

        job = _job(description="short", apply_url=None)
        db.add(job)
        db.commit()

        task = browser_tasks.enqueue(db, "resolve_link", {"url": job.url})
        task.status = "done"
        task.result = {"final_url": "https://boards.greenhouse.io/acme/jobs/3"}
        db.commit()

        ingest(db, task)
        db.refresh(job)
        assert job.apply_url == "https://boards.greenhouse.io/acme/jobs/3"


class TestEnrichmentPanel:
    """
    The house pattern: a history table plus a panel on /runs that reads it.
    Without the panel, a subsystem that quietly stops working looks identical
    to one with nothing to do.
    """

    def _run(self, **kwargs):
        from app.models.enrichment_run import EnrichmentRun

        defaults = dict(
            started_at=datetime.now(timezone.utc), status="ok",
            attempted=200, enriched=143, unchanged=41, failed=16,
            via_ats_api=61, via_json_ld=55, via_llm=22, via_landing_html=5,
            queued_browser=18, chars_gained=418233, requeued_for_matching=88,
            failures_by_host={"careers.acme.com": 9},
        )
        defaults.update(kwargs)
        return EnrichmentRun(**defaults)

    def test_the_panel_reports_what_a_pass_gained(self, client, db):
        db.add(self._run())
        db.commit()

        body = client.get("/runs").text
        assert "Enrichment" in body
        # The number the feature exists for, and the one that proves jobs got
        # a second chance rather than just a longer description.
        assert "418,233" in body
        assert "back in the matching queue" in body
        assert "61/55/22/5" in body

    def test_the_panel_names_the_hosts_refusing_us(self, client, db):
        db.add(self._run(status="partial"))
        db.commit()
        assert "careers.acme.com" in client.get("/runs").text

    def test_the_backlog_gives_the_numbers_a_denominator(self, client, db):
        db.add(self._run())
        db.add(_job(description=None, url="https://x/20",
                    source_urls=["https://x/20"]))
        db.commit()

        body = client.get("/runs").text
        assert "still have a thin or missing description" in body

    def test_the_panel_survives_having_no_history(self, client, db):
        body = client.get("/runs").text
        assert "No enrichment passes recorded yet" in body
