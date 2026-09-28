"""
A fetch cycle reads its sources side by side (`job_fetcher._run_in_lanes`),
each exactly as it ran alone, with the cycle's context in every lane. No
network: the sources are faked where `_run_adapters` calls them.
"""

import threading
import time
from unittest.mock import patch

import httpx

from app.models.profile import Profile
from app.services import job_fetcher, tunables
from app.services.sources import base


def fake(source, delay=0.0, barrier=None, seen=None):
    def fetch(query="", **kw):
        if barrier is not None:
            barrier.wait()
        if delay:
            time.sleep(delay)
        if seen is not None:
            seen.append(threading.current_thread().name)
        return [{"source": source, "title": f"{source} {query}", "url": f"https://{source}.example/1"}]
    return fetch


def run(db, overrides, only):
    db.query(Profile).delete()
    db.add(Profile(data={tunables.STORE_KEY: overrides}))
    db.commit()
    cfg = tunables.effective_settings(db.query(Profile).first().data)
    return job_fetcher._run_all_adapters(["Software Engineer"], ["Remote"], cfg, {}, {}, only=only)


class TestTheSettingsPageControlsIt:
    def test_sources_run_side_by_side(self, db):
        barrier = threading.Barrier(2, timeout=5)
        with patch("app.services.sources.remotive.fetch", fake("remotive", barrier=barrier)), \
             patch("app.services.sources.remoteok.fetch", fake("remoteok", barrier=barrier)):
            jobs, stats = run(db, {"fetch_source_concurrency": 2}, {"remotive", "remoteok"})
        assert stats["remotive"]["count"] == 1 and stats["remoteok"]["count"] == 1
        assert not stats["remotive"]["errors"] and not stats["remoteok"]["errors"]

    def test_one_reads_them_one_after_another(self, db):
        # Each waits for the other, which only a side-by-side run can satisfy.
        barrier = threading.Barrier(2, timeout=0.5)
        with patch("app.services.sources.remotive.fetch", fake("remotive", barrier=barrier)), \
             patch("app.services.sources.remoteok.fetch", fake("remoteok", barrier=barrier)):
            jobs, stats = run(db, {"fetch_source_concurrency": 1}, {"remotive", "remoteok"})
        assert stats["remotive"]["errors"] and jobs == []


def test_jobs_come_back_in_the_order_a_sequential_run_gives(db):
    only = {"remotive", "remoteok", "weworkremotely"}
    with patch("app.services.sources.remotive.fetch", fake("remotive", delay=0.3)), \
         patch("app.services.sources.remoteok.fetch", fake("remoteok", delay=0.1)), \
         patch("app.services.sources.weworkremotely.fetch", fake("weworkremotely")):
        parallel, stats = run(db, {"fetch_source_concurrency": 3}, only)
        sequential, sequential_stats = run(db, {"fetch_source_concurrency": 1}, only)
    assert [j["source"] for j in parallel] == [j["source"] for j in sequential] \
        == ["remotive", "remoteok", "weworkremotely"]
    assert list(stats) == list(sequential_stats)


def test_every_source_is_reported_and_only_the_chosen_ones_run(db):
    seen = []
    with patch("app.services.sources.remotive.fetch", fake("remotive", seen=seen)):
        _, stats = run(db, {"fetch_source_concurrency": 4}, {"remotive"})
    _, everything = job_fetcher._run_adapters(["x"], ["y"], tunables.effective_settings({}),
                                              {}, {}, only=set())
    assert set(stats) == set(everything)
    assert stats["remotive"]["enabled"] and not stats["remoteok"]["enabled"]
    assert seen and seen[0].startswith("source")       # ran in a lane's thread


def test_the_cycle_context_reaches_every_lane(db, monkeypatch):
    """Settings overlay, and the board sightings the closure step reads."""
    def board(url, params=None, **kw):
        return httpx.Response(200, json={"jobs": [{"id": 7, "title": "Engineer",
                                                   "absolute_url": "https://x/7",
                                                   "location": {"name": "Remote"}}]},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", board)
    db.query(Profile).delete()
    db.add(Profile(data={tunables.STORE_KEY: {"fetch_source_concurrency": 4,
                                              "greenhouse_descriptions_on_demand": False}}))
    db.commit()
    cfg = tunables.effective_settings(db.query(Profile).first().data)
    with base.collect_board_sightings() as sightings:
        jobs, _ = job_fetcher._run_all_adapters(["Engineer"], ["Remote"], cfg,
                                                {"greenhouse": ["acme"]}, {},
                                                only={"greenhouse"})
    assert sightings == {("greenhouse", "acme"): {"7"}}
    assert [j["source"] for j in jobs if j["source"] == "greenhouse"] == ["greenhouse"]


def test_a_lane_that_dies_does_not_take_the_others(db, monkeypatch):
    real = job_fetcher._run_adapters

    def flaky(*args, only=None, **kwargs):
        if only == {"remoteok"}:
            raise RuntimeError("boom")
        return real(*args, only=only, **kwargs)

    monkeypatch.setattr(job_fetcher, "_run_adapters", flaky)
    with patch("app.services.sources.remotive.fetch", fake("remotive")):
        jobs, stats = run(db, {"fetch_source_concurrency": 2}, {"remotive", "remoteok"})
    assert [j["source"] for j in jobs] == ["remotive"]
    assert stats["remoteok"]["errors"] == ["boom"]
