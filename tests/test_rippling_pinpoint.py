"""
Rippling and Pinpoint boards. Shapes as read on 2026-09-28 from
ats.rippling.com/flexai and wolve.pinpointhq.com. No network.
"""

import httpx
import pytest

from app.config import settings
from app.services.ats_discovery import extract_slugs
from app.services.ats_validation import probe_board
from app.services.sources import pinpoint, rippling
from app.services.sources.base import collect_board_sightings, collection_results


def _resp(url, body=None, status=200, text=None):
    req = httpx.Request("GET", url)
    if body is not None:
        return httpx.Response(status, json=body, request=req)
    return httpx.Response(status, text=text or "", request=req)


RIPPLING_ITEMS = [
    {"id": "d2b4", "name": "Senior Backend Engineer",
     "url": "https://ats.rippling.com/flexai/jobs/d2b4",
     "locations": [{"name": "Santa Clara, CA", "workplaceType": "ON_SITE"}]},
    {"id": "03ff", "name": "Office Manager",
     "url": "https://ats.rippling.com/flexai/jobs/03ff",
     "locations": [{"name": "Remote (US)", "workplaceType": "REMOTE"}]},
]


def test_rippling_lists_then_describes_the_best_titles(monkeypatch):
    described = []

    def get(url, params=None, **kw):
        if url.endswith("/jobs"):
            return _resp(url, {"items": RIPPLING_ITEMS, "totalPages": 1})
        described.append(url)
        return _resp(url, {"description": {"company": "<p>About FlexAI</p>",
                                           "role": "<p>Build the platform</p>"}})
    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(settings, "RIPPLING_DETAIL_LIMIT", 1)
    jobs, coverage, sightings = _rippling_run()
    assert len(jobs) == 2 and described == ["https://ats.rippling.com/api/v2/board/flexai/jobs/d2b4"]
    backend = next(j for j in jobs if j["title"] == "Senior Backend Engineer")
    assert "Build the platform" in backend["description"]
    assert backend["company"] == "flexai"   # the registry's name replaces it
    remote = next(j for j in jobs if j["title"] == "Office Manager")
    assert remote["is_remote"] and remote["description"] == ""
    assert coverage.status == "complete" and coverage.total == 2 and coverage.cursor is None
    assert sightings == {("rippling", "flexai"): {"d2b4", "03ff"}}


def _rippling_run(cursor=None):
    cursors = {("rippling", "flexai"): cursor} if cursor else {}
    with collection_results(cursors=cursors) as results, collect_board_sightings() as sightings:
        jobs = rippling.fetch(["flexai"], ["Backend Engineer"])
    return jobs, results[("rippling", "flexai")], sightings


def _rippling_pages(monkeypatch, responses):
    requested, described = [], []

    def get(url, params=None, **kw):
        if url.endswith("/jobs"):
            page = params["page"]
            requested.append(page)
            body = responses[page]
            if isinstance(body, httpx.Response):
                return body
            return _resp(url, body)
        described.append(url)
        return _resp(url, {"description": "<p>Build the platform</p>"})

    monkeypatch.setattr(httpx, "get", get)
    return requested, described


def test_rippling_reads_all_reported_pages_before_claiming_complete(monkeypatch):
    requested, _ = _rippling_pages(monkeypatch, {
        0: {"items": RIPPLING_ITEMS[:1], "totalPages": 2},
        1: {"items": RIPPLING_ITEMS[1:], "totalPages": 2},
    })
    jobs, coverage, sightings = _rippling_run()
    assert requested == [0, 1] and len(jobs) == 2
    assert coverage.complete is True and coverage.total == 2 and coverage.cursor is None
    assert sightings == {("rippling", "flexai"): {"d2b4", "03ff"}}


def test_rippling_empty_board_is_a_confirmed_complete_zero(monkeypatch):
    requested, described = _rippling_pages(monkeypatch, {0: {"items": [], "totalPages": 0}})
    jobs, coverage, sightings = _rippling_run()
    assert requested == [0] and not described and not jobs
    assert coverage.complete is True and coverage.total == 0 and coverage.cursor is None
    assert sightings == {("rippling", "flexai"): set()}


def test_saved_rippling_budgets_limit_work_and_resume_without_closing_from_the_tail(db, monkeypatch):
    from app.models.profile import Profile
    from app.services import tunables

    db.add(Profile(data={tunables.STORE_KEY: {"rippling_max_pages": 1, "rippling_detail_limit": 1}}))
    db.commit()
    last = {**RIPPLING_ITEMS[0], "id": "last", "url": "https://ats.rippling.com/flexai/jobs/last"}
    requested, described = _rippling_pages(monkeypatch, {
        0: {"items": RIPPLING_ITEMS, "totalPages": 2},
        1: {"items": [last], "totalPages": 2},
    })

    jobs, coverage, sightings = _rippling_run()
    assert requested == [0] and len(described) == 1 and len(jobs) == 2
    assert sum(bool(job["description"]) for job in jobs) == 1
    assert coverage.status == "partial" and coverage.cursor == {"page": 1}
    assert coverage.total is None and sightings == {}

    tail, resumed, sightings = _rippling_run(coverage.cursor)
    assert requested == [0, 1] and [job["source_job_id"] for job in tail] == ["last"]
    assert resumed.complete is False and resumed.cursor is None and resumed.total is None
    assert sightings == {} and tail[0]["_listed_open"] is True


@pytest.mark.parametrize("status, category", [(429, "rate_limited"), (503, "request_failed")])
def test_rippling_later_page_failure_preserves_collected_jobs_and_retry_page(monkeypatch, status, category):
    requested, _ = _rippling_pages(monkeypatch, {
        0: {"items": RIPPLING_ITEMS, "totalPages": 2},
        1: _resp("https://ats.rippling.com/api/v2/board/flexai/jobs", status=status),
    })
    jobs, coverage, sightings = _rippling_run()
    assert requested == [0, 1] and len(jobs) == 2
    assert coverage.status == "partial" and coverage.error_category == category
    assert coverage.cursor == {"page": 1} and coverage.total is None and sightings == {}


def test_rippling_failed_resumed_request_keeps_its_cursor(monkeypatch):
    requested, _ = _rippling_pages(monkeypatch, {
        3: _resp("https://ats.rippling.com/api/v2/board/flexai/jobs", status=503),
    })
    jobs, coverage, sightings = _rippling_run({"page": 3})
    assert requested == [3] and jobs == []
    assert coverage.status == "failed" and coverage.cursor == {"page": 3}
    assert coverage.total is None and sightings == {}


@pytest.mark.parametrize("initial, status", [(0, 404), (3, 410)])
def test_rippling_missing_later_page_restarts_without_retiring_a_live_board(monkeypatch, initial, status):
    failed_page = initial or 1
    responses = {failed_page: _resp("https://ats.rippling.com/api/v2/board/flexai/jobs", status=status)}
    if not initial:
        responses[0] = {"items": RIPPLING_ITEMS[:1], "totalPages": 2}
    requested, _ = _rippling_pages(monkeypatch, responses)

    jobs, coverage, sightings = _rippling_run({"page": initial})
    assert requested == ([3] if initial else [0, 1])
    assert len(jobs) == (0 if initial else 1)
    assert coverage.error_category == "pagination" and coverage.complete is False
    assert coverage.cursor == {"page": 0} and sightings == {}

    # A shrunken board is collected from its start, not retried indefinitely
    # at the missing page or marked as a missing employer.
    requested, _ = _rippling_pages(monkeypatch, {0: {"items": RIPPLING_ITEMS[1:], "totalPages": 1}})
    jobs, recovered, sightings = _rippling_run(coverage.cursor)
    assert requested == [0] and [job["source_job_id"] for job in jobs] == ["03ff"]
    assert recovered.complete is True and recovered.error is None and recovered.cursor is None
    assert sightings == {("rippling", "flexai"): {"03ff"}}


def test_rippling_missing_first_page_still_reports_a_missing_board(monkeypatch):
    requested, _ = _rippling_pages(monkeypatch, {
        0: _resp("https://ats.rippling.com/api/v2/board/flexai/jobs", status=404),
    })
    jobs, coverage, sightings = _rippling_run()
    assert requested == [0] and jobs == []
    assert coverage.status == "failed" and coverage.error_category == "not_found"
    assert coverage.cursor is None and sightings == {}


def test_rippling_missing_page_metadata_requires_an_empty_page_to_confirm_the_end(monkeypatch):
    requested, _ = _rippling_pages(monkeypatch, {
        0: {"items": RIPPLING_ITEMS[:1]},
        1: {"items": RIPPLING_ITEMS[1:]},
        2: {"items": []},
    })
    jobs, coverage, sightings = _rippling_run()
    assert requested == [0, 1, 2] and len(jobs) == 2
    assert coverage.complete is True and coverage.total == 2 and coverage.cursor is None
    assert sightings == {("rippling", "flexai"): {"d2b4", "03ff"}}


@pytest.mark.parametrize("second, reason", [
    ({"items": RIPPLING_ITEMS, "totalPages": 3}, "repeated a page"),
    ({"items": [], "totalPages": 3}, "empty page"),
    ({"items": [], "totalPages": "invalid"}, "invalid totalPages"),
    ({"error": "unavailable"}, "no items list"),
])
def test_rippling_broken_pagination_retains_jobs_without_claiming_complete(monkeypatch, second, reason):
    requested, _ = _rippling_pages(monkeypatch, {
        0: {"items": RIPPLING_ITEMS, "totalPages": 3}, 1: second,
    })
    jobs, coverage, sightings = _rippling_run()
    assert requested == [0, 1] and len(jobs) == 2
    assert coverage.status == "partial" and coverage.error_category == "pagination"
    assert reason in coverage.error and coverage.cursor == {"page": 1}
    assert coverage.total is None and sightings == {}


def test_rippling_malformed_posting_does_not_turn_a_partial_read_into_a_closure(monkeypatch):
    _rippling_pages(monkeypatch, {
        0: {"items": [RIPPLING_ITEMS[0], {"name": "Missing posting id"}], "totalPages": 1},
    })
    jobs, coverage, sightings = _rippling_run()
    assert [job["source_job_id"] for job in jobs] == ["d2b4"]
    assert coverage.status == "partial" and coverage.cursor == {"page": 0}
    assert coverage.total is None and sightings == {}


PINPOINT_ROW = {
    "id": "446898", "title": "C++ Software Engineer",
    "url": "https://careers.wolve.com/en/postings/856f5215",
    "description": "<p>Low-latency trading systems.</p>",
    "key_responsibilities_header": "What you'll do", "key_responsibilities": "<ul><li>C++</li></ul>",
    "location": {"name": "Chicago, IL"}, "workplace_type": "onsite",
    "employment_type": "full_time", "compensation_visible": True,
    "compensation_minimum": 150000, "compensation_maximum": 225000,
    "compensation_currency": "usd", "compensation_frequency": "year",
}


def test_pinpoint_is_one_document_with_pay(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _resp(url, {"data": [PINPOINT_ROW]}))
    (job,) = pinpoint.fetch(["wolve"])
    assert job["url"] == "https://careers.wolve.com/en/postings/856f5215"
    assert job["source_job_id"] == "wolve:446898" and job["location"] == "Chicago, IL"
    assert "Low-latency" in job["description"] and "C++" in job["description"]
    assert (job["salary_min"], job["salary_max"], job["salary_currency"], job["salary_period"]) == \
        (150000, 225000, "USD", "year")
    assert job["employment_type"] == "full_time"


def test_pinpoint_hidden_pay_stays_hidden(monkeypatch):
    row = {**PINPOINT_ROW, "compensation_visible": False}
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _resp(url, {"data": [row]}))
    (job,) = pinpoint.fetch(["wolve"])
    assert "salary_min" not in job


@pytest.mark.parametrize("url,expected", [
    ("https://ats.rippling.com/flexai/jobs/93ada67c", {"rippling": {"flexai"}}),
    ("https://ats.rippling.com/en-US/flexai/jobs", {"rippling": {"flexai"}}),
    ("https://wolve.pinpointhq.com/en/postings/e03d?ats=pinpointhq", {"pinpoint": {"wolve"}}),
])
def test_boards_are_found_in_links(url, expected):
    assert extract_slugs(url) == expected


def test_probes_want_each_boards_own_shape(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _resp(url, {"items": [], "totalPages": 0}))
    assert probe_board("rippling", "flexai").exists
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _resp(url, {"data": []}))
    assert probe_board("pinpoint", "wolve").exists
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _resp(url, status=404, text="no"))
    assert not probe_board("rippling", "nope").exists
    assert not probe_board("pinpoint", "nope").exists
