"""
A 429 from one Workday cluster rests that cluster, not the others. A fake
clock stands in for time; no network.
"""

from types import SimpleNamespace

import httpx
import pytest

from app.models.profile import Profile
from app.services import job_fetcher, tunables
from app.services.sources import base, workday


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.slept: list[float] = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(round(seconds, 1))
        self.t += seconds


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(workday, "_now", clock.now)
    monkeypatch.setattr(workday, "_sleep", clock.sleep)
    monkeypatch.setattr(workday, "_GATE", workday._ClusterGate())
    return clock


def posting(tenant):
    return {"title": "Software Engineer", "externalPath": f"/job/NYC/SWE_{tenant}",
            "locationsText": "New York", "postedOn": "Posted Today"}


def serve(monkeypatch, refusals: dict[str, int], calls: list):
    """`refusals`: cluster → how many list requests it answers 429 first."""
    left = dict(refusals)

    def post(url, **kw):
        host = url.split(".")[1]
        tenant = url.split("//")[1].split(".")[0]
        calls.append(host)
        request = httpx.Request("POST", url)
        if left.get(host, 0) > 0:
            left[host] -= 1
            return httpx.Response(429, headers={"Retry-After": "30"}, request=request)
        return httpx.Response(200, json={"total": 1, "jobPostings": [posting(tenant)]},
                              request=request)

    def get(url, **kw):
        return httpx.Response(200, json={"jobPostingInfo": {"jobDescription": "Build."}},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "get", get)


def run(specs, cooldown=60):
    cfg = SimpleNamespace(ATS_BOARD_FETCH_WORKERS=1, WORKDAY_RATE_LIMIT_COOLDOWN=cooldown)
    with base.cycle_settings(cfg):
        return workday.fetch(specs, ["Software Engineer"])


def test_a_rate_limited_cluster_rests_and_is_asked_again(monkeypatch, clock):
    calls = []
    serve(monkeypatch, {"wd5": 1}, calls)
    jobs = run(["acme:wd5:Ext", "other:wd1:Ext"])
    # The refused search was asked again after the cluster's own Retry-After.
    assert {j["company"] for j in jobs} == {"acme", "other"}
    assert clock.slept == [30.0]
    assert calls == ["wd5", "wd5", "wd1"]


def test_the_other_clusters_are_not_made_to_wait(monkeypatch, clock):
    serve(monkeypatch, {"wd5": 1}, [])
    run(["acme:wd5:Ext"])
    clock.slept.clear()
    run(["other:wd1:Ext"])
    assert clock.slept == []


def test_a_cluster_that_keeps_refusing_is_left_for_the_cycle(monkeypatch, clock):
    calls = []
    serve(monkeypatch, {"wd5": 99}, calls)
    jobs = run(["acme:wd5:Ext", "beta:wd5:Ext", "other:wd1:Ext"])
    assert [j["company"] for j in jobs] == ["other"]
    assert calls.count("wd5") == workday._MAX_TRIPS_PER_CYCLE
    # And a new cycle tries it again.
    calls.clear()
    serve(monkeypatch, {}, calls)
    assert [j["company"] for j in run(["acme:wd5:Ext"])] == ["acme"]


def test_retry_after_as_a_date_and_missing():
    ok = httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})
    assert workday._retry_after(ok, 60) == 0.0   # already passed
    assert workday._retry_after(httpx.Response(429), 45) == 45


class TestTheSettingsPageControlsIt:
    def _run(self, db, overrides):
        db.query(Profile).delete()
        db.add(Profile(data={tunables.STORE_KEY: {"ats_board_fetch_workers": 1, **overrides}}))
        db.commit()
        cfg = tunables.effective_settings(db.query(Profile).first().data)
        return job_fetcher._run_all_adapters(
            ["Software Engineer"], ["Remote"], cfg, {"workday": ["acme:wd5:Ext"]}, {},
            only={"workday"})

    def test_the_rest_is_the_settings_pages(self, db, monkeypatch, clock):
        monkeypatch.setattr(workday, "_retry_after", lambda resp, default: default)
        serve(monkeypatch, {"wd5": 1}, [])
        self._run(db, {"workday_rate_limit_cooldown": 90})
        assert clock.slept == [90.0]

    def test_zero_moves_on_as_before(self, db, monkeypatch, clock):
        calls = []
        serve(monkeypatch, {"wd5": 1}, calls)
        jobs, _ = self._run(db, {"workday_rate_limit_cooldown": 0})
        assert clock.slept == [] and calls == ["wd5"] and jobs == []


def test_a_refused_description_rests_the_cluster_too(monkeypatch, clock):
    serve(monkeypatch, {}, [])

    def get(url, **kw):
        return httpx.Response(429, headers={"Retry-After": "20"}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)
    [job] = run(["acme:wd5:Ext"])
    assert job["description"] == ""          # left for enrichment
    run(["beta:wd5:Ext"])
    assert clock.slept == [20.0]
