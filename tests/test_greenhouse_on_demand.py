"""
Greenhouse boards read without their text, the text fetched only for postings
not already stored with it. No network.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest

from app.models.job import Job
from app.models.profile import Profile
from app.services import job_fetcher, tunables
from app.services.sources import base, greenhouse

NOW = datetime.now(timezone.utc)


def item(n, age_days=1):
    return {"id": 5000 + n, "title": f"Software Engineer {n}",
            "absolute_url": f"https://job-boards.greenhouse.io/acme/jobs/{5000 + n}",
            "location": {"name": "Remote"},
            "first_published": (NOW - timedelta(days=age_days)).isoformat()}


class Board:
    def __init__(self, monkeypatch, items):
        self.items = items
        self.calls: list[str] = []
        monkeypatch.setattr(httpx, "get", self.get)

    def get(self, url, params=None, **kw):
        request = httpx.Request("GET", url)
        with_text = bool(params and params.get("content"))
        self.calls.append(url + ("?content=true" if with_text else ""))
        if "/jobs/" in url:
            job_id = int(url.rsplit("/", 1)[1])
            return httpx.Response(200, json={"id": job_id, "content": f"&lt;p&gt;Text {job_id}&lt;/p&gt;"},
                                  request=request)
        jobs = [{**i, "content": f"&lt;p&gt;Text {i['id']}&lt;/p&gt;"} if with_text else i
                for i in self.items]
        return httpx.Response(200, json={"jobs": jobs}, request=request)

    def postings_read(self):
        return [c for c in self.calls if "/jobs/" in c]

    def full_reads(self):
        return [c for c in self.calls if c.endswith("?content=true")]


def run(known=(), max_age_days=30):
    with base.known_descriptions({"greenhouse": set(known)}):
        return greenhouse.fetch(["acme"], max_age_days=max_age_days)


def test_only_new_postings_have_their_text_fetched(monkeypatch):
    board = Board(monkeypatch, [item(1), item(2), item(3)])
    jobs = {j["source_job_id"]: j for j in run(known={"5001", "5002"})}
    assert board.postings_read() == ["https://boards-api.greenhouse.io/v1/boards/acme/jobs/5003"]
    assert board.full_reads() == []
    assert "Text 5003" in jobs["5003"]["description"]
    # Known ones come back without text: the save finds the stored row.
    assert jobs["5001"]["description"] == "" and len(jobs) == 3


def test_a_board_mostly_new_is_read_once_with_its_text(monkeypatch):
    board = Board(monkeypatch, [item(n) for n in range(30)])
    jobs = run(known={"5000"})
    assert board.postings_read() == [] and len(board.full_reads()) == 1
    assert all(j["description"] for j in jobs if j["source_job_id"] != "5000")


def test_a_posting_too_old_to_keep_is_not_fetched(monkeypatch):
    board = Board(monkeypatch, [item(1), item(2, age_days=90)])
    assert [j["source_job_id"] for j in run()] == ["5001"]
    assert board.postings_read() == ["https://boards-api.greenhouse.io/v1/boards/acme/jobs/5001"]


def test_a_text_that_will_not_load_leaves_the_posting_for_enrichment(monkeypatch):
    board = Board(monkeypatch, [item(1)])
    original = board.get

    def get(url, params=None, **kw):
        if "/jobs/" in url:
            return httpx.Response(500, request=httpx.Request("GET", url))
        return original(url, params=params, **kw)

    monkeypatch.setattr(httpx, "get", get)
    [job] = run()
    assert job["description"] == ""


class TestTheSettingsPageControlsIt:
    def _run(self, db, overrides):
        db.query(Profile).delete()
        db.add(Profile(data={tunables.STORE_KEY: overrides}))
        db.commit()
        cfg = tunables.effective_settings(db.query(Profile).first().data)
        return job_fetcher._run_all_adapters(
            ["Software Engineer"], ["Remote"], cfg, {"greenhouse": ["acme"]}, {},
            only={"greenhouse"})

    def test_off_reads_every_board_with_its_text(self, db, monkeypatch):
        board = Board(monkeypatch, [item(1), item(2)])
        self._run(db, {"greenhouse_descriptions_on_demand": False})
        assert len(board.full_reads()) == 1 and board.postings_read() == []
        board.calls.clear()
        self._run(db, {})
        assert board.full_reads() == [] and len(board.postings_read()) == 2


def test_a_second_cycle_downloads_no_text_it_already_has(db, monkeypatch):
    from tests.test_fetch_task import _make_profile_with_targets

    _make_profile_with_targets(db)
    board = Board(monkeypatch, [item(1), item(2)])

    def cycle():
        def run_adapters(*args, **kwargs):
            jobs = greenhouse.fetch(["acme"], max_age_days=30)
            return jobs, {"greenhouse": {"count": len(jobs), "errors": [], "enabled": True}}

        with patch("app.services.query_expansion.expand_search_queries",
                   return_value=(["Software Engineer"], None)), \
             patch("app.services.job_fetcher._run_all_adapters", side_effect=run_adapters):
            job_fetcher.fetch_and_save_jobs(db)

    cycle()
    assert len(board.postings_read()) == 2
    stored = db.query(Job).filter_by(source="greenhouse").all()
    # Long enough to be trusted next time.
    for job in stored:
        job.description = (job.description or "") + " Build things." * 30
    db.commit()

    board.calls.clear()
    board.items.append(item(3))
    cycle()
    assert board.postings_read() == ["https://boards-api.greenhouse.io/v1/boards/acme/jobs/5003"]
    assert all(len(j.description) > 200 for j in db.query(Job).filter(
        Job.source == "greenhouse", Job.source_job_id.in_(["5001", "5002"])))
