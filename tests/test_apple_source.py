"""
Apple's careers search, read by the server. Pages embed their data the way
jobs.apple.com's did on 2026-09-28: `window.__staticRouterHydrationData =
JSON.parse("…")`. No network.
"""

import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.models.profile import Profile
from app.services import job_fetcher, tunables
from app.services.sources import apple
from app.services.sources.base import SourceUnavailable

AUSTIN = {"name": "Austin", "city": "", "stateProvince": "",
          "countryName": "United States of America"}
SEATTLE = {"name": "Seattle", "city": "", "stateProvince": "",
           "countryName": "United States of America"}


@pytest.fixture(autouse=True)
def _fresh_detail_memory():
    apple._DETAILED.clear()
    yield
    apple._DETAILED.clear()


def page(loader: dict) -> str:
    blob = json.dumps(json.dumps({"loaderData": loader}))
    return f"<html><script>window.__staticRouterHydrationData = JSON.parse({blob});</script></html>"


def result(n, title=None, location=AUSTIN, **over):
    return {
        "id": f"{200667700 + n}-0157", "positionId": str(200667700 + n),
        "postingTitle": title or f"Software Engineer {n}",
        "transformedPostingTitle": f"software-engineer-{n}",
        "jobSummary": "At Apple, our mission is simple.",
        "postDateInGMT": "2026-09-28T01:31:13.686Z", "locations": [location],
        "homeOffice": False, **over,
    }


def details(n):
    return {
        "positionId": str(200667700 + n), "jobSummary": "At Apple, our mission is simple.",
        "description": "You will own the test platform.",
        "responsibilities": "Build automation frameworks.",
        "minimumQualifications": "BS in Computer Science.",
        "preferredQualifications": "Swift.",
        "locations": [{"name": "Austin", "city": "Austin", "stateProvince": "Texas",
                       "countryName": "United States of America"}],
    }


def serve(monkeypatch, results, calls, status=200, total=None, per_page=20):
    def get(url, params=None, **kw):
        calls.append((url, dict(params or {})))
        request = httpx.Request("GET", url)
        if status != 200:
            return httpx.Response(status, text="slow down", request=request)
        if "/details/" in url:
            n = int(url.split("/details/")[1].split("/")[0]) - 200667700
            return httpx.Response(200, text=page({"jobDetails": {"jobsData": details(n)}}),
                                  request=request)
        start = (int(params["page"]) - 1) * per_page
        return httpx.Response(200, text=page({"search": {
            "searchResults": results[start:start + per_page],
            "totalRecords": len(results) if total is None else total,
        }}), request=request)
    monkeypatch.setattr(httpx, "get", get)


def _searches(calls):
    return [params for url, params in calls if url.endswith("/search")]


def test_a_matching_posting_is_read_whole(monkeypatch):
    calls = []
    serve(monkeypatch, [result(1)], calls)
    [job] = apple.fetch("software engineer")
    assert job["company"] == "Apple" and job["source_job_id"] == "200667701"
    assert job["url"] == "https://jobs.apple.com/en-us/details/200667701/software-engineer-1"
    assert job["posted_at"] == "2026-09-28T01:31:13.686Z"
    assert job["location"] == "Austin, Texas, United States"
    for text in ("own the test platform", "automation frameworks", "BS in Computer Science", "Swift"):
        assert text in job["description"]
    assert _searches(calls)[0] == {"search": "software engineer", "sort": "newest",
                                   "location": "united-states-USA", "page": 1}


def test_the_detail_budget_goes_to_the_titles_that_match(monkeypatch):
    calls = []
    rows = [result(1, title="Retail Specialist"), result(2, title="Software Engineer, Maps")]
    serve(monkeypatch, rows, calls)
    jobs = {j["title"]: j for j in apple.fetch("software engineer", max_details=1)}
    assert "BS in Computer Science" in jobs["Software Engineer, Maps"]["description"]
    assert jobs["Retail Specialist"]["description"] == "At Apple, our mission is simple."
    assert [u for u, _ in calls if "/details/" in u] == [
        "https://jobs.apple.com/en-us/details/200667702/software-engineer-2"]


def test_a_detail_already_read_is_not_read_again(monkeypatch):
    calls = []
    serve(monkeypatch, [result(1), result(2)], calls)
    apple.fetch("software engineer", max_details=1)
    apple.fetch("software engineer", max_details=1)
    details_read = [u for u, _ in calls if "/details/" in u]
    # The second run spends its one detail on the posting the first could not.
    assert len(details_read) == 2 and len(set(details_read)) == 2


def test_enrichment_reads_the_rest_from_the_same_page():
    from unittest.mock import MagicMock

    from app.services import enrichment

    url = "https://jobs.apple.com/en-us/details/200667701/software-engineer-1"
    assert enrichment.looks_like_ats(url)
    resp = MagicMock(status_code=200, text=page({"jobDetails": {"jobsData": details(1)}}))
    client = MagicMock()
    client.get.return_value = resp
    found = enrichment.enrich_one(client, url)
    assert client.get.call_args[0][0] == url
    assert "BS in Computer Science" in found.description and found.method == "ats_api"


def test_one_position_in_several_places_is_one_job(monkeypatch):
    rows = [result(1), {**result(1), "id": "200667701-0836", "locations": [SEATTLE]}]
    serve(monkeypatch, rows, [])
    [job] = apple.fetch("software engineer", max_details=0)
    assert job["location"] == "Austin, United States; Seattle, United States"


def test_pages_until_the_total_runs_out(monkeypatch):
    calls = []
    serve(monkeypatch, [result(n) for n in range(45)], calls)
    jobs = apple.fetch("software engineer", max_pages=10, max_details=0)
    assert len(jobs) == 45 and [p["page"] for p in _searches(calls)] == [1, 2, 3]


def test_a_result_from_another_country_is_dropped(monkeypatch):
    """An unknown location slug returns US postings rather than none."""
    toronto = {"name": "Toronto", "countryName": "Canada"}
    serve(monkeypatch, [result(1), result(2, location=toronto)], [])
    jobs = apple.fetch("software engineer", location="canada-CANC", max_details=0)
    assert [j["source_job_id"] for j in jobs] == ["200667702"]


def test_an_internship_says_so(monkeypatch):
    serve(monkeypatch, [result(1, title="Software Engineering Internships")], [])
    [job] = apple.fetch("software engineer", max_details=0)
    assert job["employment_type"] == "internship"


def test_a_page_without_its_data_is_an_error(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Response(
        200, text="<html>maintenance</html>", request=httpx.Request("GET", url)))
    with pytest.raises(ValueError):
        apple.fetch("software engineer")


def test_a_rate_limit_stops_the_source(monkeypatch):
    serve(monkeypatch, [], [], status=429)
    with pytest.raises(SourceUnavailable):
        apple.fetch("software engineer")


@pytest.mark.parametrize("codes,expected", [
    ([], ["united-states-USA"]), (["us", "ca"], ["united-states-USA", "canada-CANC"]),
    (["jp"], ["united-states-USA"]),
])
def test_countries_follow_the_profile(codes, expected):
    assert apple.countries_for(codes) == expected


class TestTheSettingsPageControlsIt:
    def _run(self, db, overrides):
        db.query(Profile).delete()
        db.add(Profile(data={tunables.STORE_KEY: overrides}))
        db.commit()
        cfg = tunables.effective_settings(db.query(Profile).first().data)
        return job_fetcher._run_all_adapters(
            ["Software Engineer"], ["Remote"], cfg, {}, {}, only={"apple"})

    def test_pages_per_search(self, db, monkeypatch):
        calls = []
        serve(monkeypatch, [result(n) for n in range(100)], calls)
        self._run(db, {"apple_max_pages": 1, "apple_max_details": 0})
        assert len(_searches(calls)) == 1
        calls.clear()
        self._run(db, {"apple_max_pages": 4, "apple_max_details": 0})
        assert len(_searches(calls)) == 4

    def test_full_descriptions_per_search(self, db, monkeypatch):
        calls = []
        serve(monkeypatch, [result(n) for n in range(10)], calls)
        self._run(db, {"apple_max_details": 3})
        assert len([u for u, _ in calls if "/details/" in u]) == 3
        calls.clear()
        self._run(db, {"apple_max_details": 0})
        assert [u for u, _ in calls if "/details/" in u] == []

    def test_switching_it_off(self, db, monkeypatch):
        calls = []
        serve(monkeypatch, [result(1)], calls)
        _, stats = self._run(db, {"apple_enabled": False})
        assert calls == [] and stats["apple"]["enabled"] is False
