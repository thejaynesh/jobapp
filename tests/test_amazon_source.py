"""
Amazon's careers search, read by the server. Items are shaped like
`amazon.jobs/en/search.json` as read on 2026-09-28. No network.
"""

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.models.profile import Profile
from app.services import job_fetcher, tunables
from app.services.sources import amazon
from app.services.sources.base import SourceUnavailable


def item(n, **over):
    return {
        "id_icims": str(10560000 + n), "title": f"Software Development Engineer {n}",
        "job_path": f"/en/jobs/{10560000 + n}/software-development-engineer",
        "posted_date": "September 25, 2026", "normalized_location": "Seattle, Washington, USA",
        "company_name": "Amazon.com Services LLC", "is_intern": False,
        "description": "Build services at scale.",
        "basic_qualifications": "- 3+ years of software development<br/>- Java",
        "preferred_qualifications": "- Distributed systems",
        **over,
    }


def serve(monkeypatch, total, calls, status=200):
    def get(url, params=None, **kw):
        calls.append(dict(params or {}))
        if status != 200:
            return httpx.Response(status, text="slow down", request=httpx.Request("GET", url))
        offset, limit = int(params["offset"]), int(params["result_limit"])
        rows = [item(n) for n in range(offset, min(offset + limit, total))]
        return httpx.Response(200, json={"hits": total, "jobs": rows},
                              request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx, "get", get)


def test_a_posting_is_read_whole(monkeypatch):
    serve(monkeypatch, 1, [])
    (job,) = amazon.fetch("software engineer")
    assert job["company"] == "Amazon" and job["source_job_id"] == "10560000"
    assert job["url"] == "https://www.amazon.jobs/en/jobs/10560000/software-development-engineer"
    assert job["posted_at"] == "2026-09-25T00:00:00+00:00"
    assert job["location"] == "Seattle, Washington, USA"
    assert "Build services at scale." in job["description"]
    assert "Basic qualifications" in job["description"] and "Java" in job["description"]


def test_pages_until_the_hits_run_out(monkeypatch):
    calls = []
    serve(monkeypatch, 150, calls)
    jobs = amazon.fetch("software engineer", max_pages=5)
    assert [c["offset"] for c in calls] == [0, 100] and len(jobs) == 150
    assert calls[0]["country"] == "USA" and calls[0]["sort"] == "recent"


def test_an_intern_posting_says_so(monkeypatch):
    def get(url, params=None, **kw):
        return httpx.Response(200, json={"hits": 1, "jobs": [item(1, is_intern=True)]},
                              request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx, "get", get)
    (job,) = amazon.fetch("software engineer")
    assert job["employment_type"] == "internship"


def test_a_rate_limit_stops_the_source(monkeypatch):
    serve(monkeypatch, 1, [], status=429)
    with pytest.raises(SourceUnavailable):
        amazon.fetch("software engineer")


@pytest.mark.parametrize("codes,expected", [
    ([], ["USA"]), (["us"], ["USA"]), (["us", "ca"], ["USA", "CAN"]), (["zz"], ["USA"]),
])
def test_countries_follow_the_profile(codes, expected):
    assert amazon.countries_for(codes) == expected


class TestTheSettingsPageControlsIt:
    def _run(self, db, overrides):
        db.query(Profile).delete()
        db.add(Profile(data={tunables.STORE_KEY: overrides}))
        db.commit()
        cfg = tunables.effective_settings(db.query(Profile).first().data)
        return job_fetcher._run_all_adapters(
            ["Software Engineer"], ["Remote"], cfg, {}, {}, only={"amazon"})

    def test_pages_per_search(self, db, monkeypatch):
        calls = []
        serve(monkeypatch, 1000, calls)
        self._run(db, {"amazon_max_pages": 1})
        assert len(calls) == 1
        calls.clear()
        self._run(db, {"amazon_max_pages": 3})
        assert len(calls) == 3

    def test_switching_it_off(self, db, monkeypatch):
        calls = []
        serve(monkeypatch, 10, calls)
        _, stats = self._run(db, {"amazon_enabled": False})
        assert calls == [] and stats["amazon"]["enabled"] is False
