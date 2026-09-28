"""
Rippling and Pinpoint boards. Shapes as read on 2026-09-28 from
ats.rippling.com/flexai and wolve.pinpointhq.com. No network.
"""

import httpx
import pytest

from app.services.ats_discovery import extract_slugs
from app.services.ats_validation import probe_board
from app.services.sources import pinpoint, rippling


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
    monkeypatch.setattr(rippling, "_MAX_DETAILS", 1)
    jobs = rippling.fetch(["flexai"], ["Backend Engineer"])
    assert len(jobs) == 2 and described == ["https://ats.rippling.com/api/v2/board/flexai/jobs/d2b4"]
    backend = next(j for j in jobs if j["title"] == "Senior Backend Engineer")
    assert "Build the platform" in backend["description"]
    assert backend["company"] == "flexai"   # the registry's name replaces it
    remote = next(j for j in jobs if j["title"] == "Office Manager")
    assert remote["is_remote"] and remote["description"] == ""


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
