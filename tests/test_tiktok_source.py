"""
TikTok's careers search, read by the server. Rows and the filter list are
shaped like api.lifeattiktok.com's as read on 2026-09-28. No network.
"""

import json

import httpx
import pytest

from app.models.profile import Profile
from app.services import job_fetcher, tunables
from app.services.sources import tiktok
from app.services.sources.base import SourceUnavailable


def _place(code, name, parent=None):
    return {"code": code, "location_type": 3 if code.startswith("CT") else 1,
            "name": None, "en_name": name, "i18n_name": name, "parent": parent}


US = _place("CN_6", "United States of America")
SAN_JOSE = _place("CT_1103355", "San Jose", _place("ST_31", "California", US))
SEATTLE = _place("CT_157", "Seattle", _place("ST_48", "Washington", US))
SINGAPORE = _place("CT_163", "Singapore", _place("ST_100785", "Singapore",
                                                 _place("CN_25", "Singapore")))
BERLIN = _place("CT_6", "Berlin", _place("ST_9", "Berlin", _place("CN_4", "Germany")))
CITIES = [SAN_JOSE, SEATTLE, SINGAPORE, BERLIN]


def row(n, kind="101", city=SAN_JOSE, **over):
    names = {"101": "Regular", "201": "Regular", "202": "Intern"}
    return {
        "id": str(7611425885984393525 + n), "code": f"A{n:05d}",
        "title": f"Software Engineer {n}",
        "description": "Build the recommendation platform.\n- Own services end to end",
        "requirement": "Minimum Qualifications\n- BS in Computer Science",
        "recruit_type": {"id": kind, "name": None, "en_name": names[kind]},
        "city_info": city, "job_subject": None,
        **over,
    }


def serve(monkeypatch, total, calls, status=200, fail_at=None):
    def post(url, json=None, **kw):
        calls.append((url.rsplit("/supplier", 1)[1], json))
        request = httpx.Request("POST", url)
        if url.endswith("/config/job/filters"):
            return httpx.Response(200, json={"code": 0, "data": {"city_list": CITIES}},
                                  request=request)
        if status != 200:
            return httpx.Response(status, text="slow down", request=request)
        offset, limit = json["offset"], json["limit"]
        if fail_at is not None and offset >= fail_at:
            raise httpx.ConnectTimeout("handshake timed out")
        rows = [row(n) for n in range(offset, min(offset + limit, total))]
        return httpx.Response(200, json={"code": 0, "data": {"job_post_list": rows,
                                                              "count": total}},
                              request=request)
    monkeypatch.setattr(httpx, "post", post)


class TestCities:
    def test_only_cities_in_the_profiles_countries(self, monkeypatch):
        serve(monkeypatch, 0, [])
        assert tiktok.city_codes(["us"]) == ["CT_1103355", "CT_157"]
        assert tiktok.city_codes(["de"]) == ["CT_6"]
        assert tiktok.city_codes(["us", "sg"]) == ["CT_1103355", "CT_157", "CT_163"]

    @pytest.mark.parametrize("codes", [[], None, ["zz"]])
    def test_the_us_when_none_it_knows(self, monkeypatch, codes):
        serve(monkeypatch, 0, [])
        assert tiktok.city_codes(codes) == ["CT_1103355", "CT_157"]

    def test_a_country_without_an_office_searches_nothing(self, monkeypatch):
        calls = []
        serve(monkeypatch, 0, calls)
        assert tiktok.city_codes(["ca"]) == []
        assert tiktok.fetch("software engineer", []) == [] and len(calls) == 1


def test_a_posting_is_read_whole(monkeypatch):
    calls = []
    serve(monkeypatch, 1, calls)
    [job] = tiktok.fetch("software engineer", ["CT_1103355"])
    assert job["title"] == "Software Engineer 0" and job["company"] == "TikTok"
    assert job["url"] == "https://lifeattiktok.com/search/7611425885984393525"
    assert job["source_job_id"] == "7611425885984393525"
    assert job["location"] == "San Jose, California, United States"
    assert "Own services end to end" in job["description"]
    assert "BS in Computer Science" in job["description"]
    assert job["posted_at"] is None
    body = calls[-1][1]
    assert body["keyword"] == "software engineer"
    assert body["location_code_list"] == ["CT_1103355"]


def test_one_place_named_three_times_is_named_once():
    assert tiktok._location(row(0, city=SINGAPORE)) == "Singapore"


@pytest.mark.parametrize("kind,level,employment", [
    ("201", "entry", None),          # campus graduate
    ("202", "entry", "internship"),  # campus intern
])
def test_campus_hiring_is_entry_level(kind, level, employment):
    job = tiktok._as_job(row(0, kind=kind, title="Backend Engineer"))
    assert job["experience_level"] == level
    assert job.get("employment_type") == employment


def test_an_experienced_role_is_read_from_its_title():
    job = tiktok._as_job(row(0, kind="101", title="Senior Backend Engineer"))
    assert job["experience_level"] == "senior" and "employment_type" not in job


def test_pages_until_the_count_runs_out(monkeypatch):
    calls = []
    serve(monkeypatch, 250, calls)
    jobs = tiktok.fetch("software engineer", ["CT_157"], max_pages=10)
    assert len(jobs) == 250
    assert [body["offset"] for _, body in calls] == [0, 100, 200]


def test_a_later_page_failing_keeps_the_earlier_ones(monkeypatch):
    serve(monkeypatch, 250, [], fail_at=100)
    assert len(tiktok.fetch("software engineer", ["CT_157"], max_pages=5)) == 100


def test_a_first_page_failing_is_reported(monkeypatch):
    serve(monkeypatch, 250, [], fail_at=0)
    with pytest.raises(httpx.ConnectTimeout):
        tiktok.fetch("software engineer", ["CT_157"])


def test_a_rate_limit_stops_the_source(monkeypatch):
    serve(monkeypatch, 10, [], status=429)
    with pytest.raises(SourceUnavailable):
        tiktok.fetch("software engineer", ["CT_157"])


def test_an_api_error_is_an_error(monkeypatch):
    def post(url, json=None, **kw):
        return httpx.Response(200, json={"code": -9000002, "data": None,
                                         "message": "Server request failed"},
                              request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(RuntimeError, match="-9000002"):
        tiktok.search("x", ["CT_157"])


class TestTheSettingsPageControlsIt:
    def _run(self, db, overrides):
        db.query(Profile).delete()
        db.add(Profile(data={tunables.STORE_KEY: overrides}))
        db.commit()
        cfg = tunables.effective_settings(db.query(Profile).first().data)
        return job_fetcher._run_all_adapters(
            ["Software Engineer"], ["Remote"], cfg, {}, {}, only={"tiktok"})

    def _searches(self, calls):
        return [body for path, body in calls if path == "/search/job/posts"]

    def test_pages_per_search(self, db, monkeypatch):
        calls = []
        serve(monkeypatch, 1000, calls)
        self._run(db, {"tiktok_max_pages": 1})
        assert len(self._searches(calls)) == 1
        calls.clear()
        self._run(db, {"tiktok_max_pages": 3})
        assert len(self._searches(calls)) == 3

    def test_switching_it_off(self, db, monkeypatch):
        calls = []
        serve(monkeypatch, 10, calls)
        _, stats = self._run(db, {"tiktok_enabled": False})
        assert calls == [] and stats["tiktok"]["enabled"] is False

    def test_searches_are_restricted_to_the_us_by_default(self, db, monkeypatch):
        calls = []
        serve(monkeypatch, 3, calls)
        jobs, stats = self._run(db, {})
        assert stats["tiktok"]["count"] == 3
        assert {json.dumps(b["location_code_list"]) for b in self._searches(calls)} == {
            json.dumps(["CT_1103355", "CT_157"])}
