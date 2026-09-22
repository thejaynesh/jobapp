"""Regression cases for public formats and bounded pagination (no network)."""

from unittest.mock import Mock

import httpx
import pytest

from app.services.sources import builtin, himalayas, icims, jobspresso, themuse, workday
from app.services.sources.listing_fallbacks import extract_listing_jobs


BUILTIN = '''<div data-id="job-card"><img src="logo.png">
<a data-id="company-title" href="/company/acme"><span>Acme</span></a>
<h2><a data-id="job-card-title" href="/job/software-engineer/123">Software Engineer</a></h2>
<div><div><i class="fa-house-building"></i></div><span>Remote</span></div>
<div><div><i class="fa-location-dot"></i></div><div><span>Canada</span></div></div>
<div><span>150K</span><span>Senior level</span></div></div>'''
ICIMS = '''<li class="iCIMS_JobCardItem"><div class="row">
<div class="col-xs-6 header left"><span class="sr-only field-label">Job Locations</span>
<span>US-VA</span></div><div class="header right">2 days ago</div>
<div class="title"><a href="/jobs/42/software-engineer/job?in_iframe=1">
<span class="sr-only">Job Posting Title</span><h3>Software Engineer</h3></a></div>
<div class="additionalFields">ID 2026-42</div></div></li>'''


def response(url="https://example.com", text="", data=None):
    return httpx.Response(200, text=text, request=httpx.Request("GET", url)) if data is None else (
        httpx.Response(200, json=data, request=httpx.Request("GET", url)))


def test_builtin_cards_preserve_identity_location_and_dedupe(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: response("https://builtin.com/jobs", BUILTIN))
    jobs = builtin.fetch("Software Engineer")
    assert len(jobs) == 1  # Same posting on search and remote pages.
    assert jobs[0]["company"] == "Acme"
    assert jobs[0]["source_job_id"] == "123"
    assert jobs[0]["location"] == "Canada; Remote"
    assert jobs[0]["is_remote"]
    assert jobs[0]["description"] == "" and jobs[0]["posted_at"] is None
    assert builtin.fetch("Nurse") == []


@pytest.mark.parametrize("source,html,url", [
    ("builtin", BUILTIN.replace('/job/software-engineer/123', 'https://evil.test/job/software-engineer/123'),
     "https://builtin.com/jobs"),
    ("icims", ICIMS.replace('/jobs/42/', 'https://evil.test/jobs/42/'), "https://acme.icims.com/jobs/search"),
])
def test_cards_reject_foreign_links(source, html, url):
    assert extract_listing_jobs(html, url, source, "acme") == []


@pytest.mark.parametrize("slug", ["acme", "acme.icims.com"])
def test_icims_fetch_and_validation_use_inner_listing(monkeypatch, slug):
    from app.services.ats_validation import probe_board

    calls = []

    def get(url, **kw):
        calls.append(url)
        assert "in_iframe=1" in url and "acme.icims.com/jobs/search" in url
        return response(url, ICIMS)

    monkeypatch.setattr(httpx, "get", get)
    assert probe_board("icims", slug).exists
    jobs = icims.fetch([slug])
    assert len(jobs) == 1 and len(calls) == 2
    assert jobs[0]["title"] == "Software Engineer"
    assert jobs[0]["location"] == "US-VA"
    assert jobs[0]["url"] == "https://acme.icims.com/jobs/42/software-engineer/job"
    assert jobs[0]["source_job_id"] == f"{slug}:42"


@pytest.mark.parametrize("date,expected", [
    ("Sat, 29 Aug 2026 02:12:12 +0000", "2026-08-29T02:12:12+00:00"), ("bad", None),
])
def test_jobspresso_job_feed_has_company_restrictions_and_parseable_date(monkeypatch, date, expected):
    feed = f'''<rss xmlns:job="https://jobspresso.co"><channel><item>
    <title>Software Engineer - Backend</title><link>https://jobspresso.co/job/backend/</link>
    <guid>https://jobspresso.co/?post_type=job_listing&amp;p=123</guid>
    <description>Build APIs</description><job:company>Acme</job:company>
    <job:location>Canada</job:location><pubDate>{date}</pubDate></item></channel></rss>'''
    get = Mock(return_value=response(text=feed))
    monkeypatch.setattr(httpx, "get", get)
    job = jobspresso.fetch("Engineer")[0]
    assert "feed=job_feed" in get.call_args.args[0]
    assert job["title"] == "Software Engineer - Backend"
    assert (job["company"], job["location"], job["source_job_id"]) == ("Acme", "Canada", "123")
    assert job["posted_at"] == expected


def test_muse_includes_first_page_and_stops_at_page_count(monkeypatch):
    monkeypatch.setattr(themuse, "_CATEGORIES", ["Software Engineering"])
    get = Mock(return_value=response(data={"results": [{"id": 1, "name": "Engineer"}], "page_count": 1}))
    monkeypatch.setattr(httpx, "get", get)
    assert len(themuse.fetch("Engineer")) == 1
    assert get.call_count == 1 and get.call_args.kwargs["params"]["page"] == 0


def test_himalayas_short_pages_continue_but_repeats_stop(monkeypatch):
    monkeypatch.setattr(himalayas, "_MAX_PAGES", 10)
    calls = []

    def get(url, params, **kw):
        calls.append(params)
        assert url.endswith("/search") and params["q"] == "Engineer"
        job_id = min(params["page"], 2)
        return response(data={"jobs": [{"title": "Engineer", "guid": f"https://example.com/{job_id}"}]})

    monkeypatch.setattr(httpx, "get", get)
    assert len(himalayas.fetch("Engineer")) == 2
    assert [p["page"] for p in calls] == [1, 2, 3]


def test_himalayas_request_budget_bounds_unique_pages(monkeypatch):
    calls = []

    def get(url, params, **kw):
        calls.append(params["page"])
        return response(data={"jobs": [{"title": "Engineer", "guid": f'https://example.com/{params["page"]}'}]})

    monkeypatch.setattr(httpx, "get", get)
    assert len(himalayas.fetch("Engineer")) == 3
    assert calls == [1, 2, 3]


def test_himalayas_keeps_earlier_pages_on_error(monkeypatch):
    get = Mock(side_effect=[response(data={"jobs": [{"title": "Engineer", "guid": "https://example.com/1"}]}),
                           httpx.ReadTimeout("timeout")])
    monkeypatch.setattr(httpx, "get", get)
    assert len(himalayas.fetch("Engineer")) == 1


def test_workday_covers_all_roles_then_pages_with_bounded_details(monkeypatch):
    calls = []

    def post(url, json, **kw):
        calls.append((json["searchText"], json["offset"]))
        rows = [{"title": "Engineer", "externalPath": f'/job/{json["searchText"]}-{i}'}
                for i in range(json["offset"], json["offset"] + 20)]
        return response(data={"total": 500, "jobPostings": rows})

    monkeypatch.setattr(httpx, "post", post)
    details = Mock(return_value={})
    monkeypatch.setattr(workday, "_fetch_detail", details)
    jobs = workday.fetch(["acme:wd1:careers"], [f"role{i}" for i in range(10)])
    assert len(jobs) == 400
    assert calls[:10] == [(f"role{i}", 0) for i in range(10)]
    assert calls[10:] == [(f"role{i}", 20) for i in range(10)]
    assert len(calls) == 20 and details.call_count == 20


@pytest.mark.parametrize("mode,expected_calls", [("short", 1), ("total", 1), ("repeat", 2), ("error", 2), ("many", 3)])
def test_workday_pagination_stops_and_keeps_partial_results(monkeypatch, mode, expected_calls):
    def post(url, json, **kw):
        offset = json["offset"]
        if mode == "error" and offset:
            raise httpx.ReadTimeout("timeout")
        start = 0 if mode == "repeat" else offset
        rows = [{"title": "Engineer", "externalPath": f"/job/{i}"}
                for i in range(start, start + (1 if mode == "short" else 20))]
        return response(data={"total": 20 if mode == "total" else 500, "jobPostings": rows})

    post_mock = Mock(side_effect=post)
    monkeypatch.setattr(httpx, "post", post_mock)
    monkeypatch.setattr(workday, "_fetch_detail", lambda *a: {})
    jobs = workday.fetch(["acme:wd1:careers"], ["Engineer"])
    assert jobs
    assert post_mock.call_count == expected_calls
    assert len(jobs) == (1 if mode == "short" else 60 if mode == "many" else 20)
