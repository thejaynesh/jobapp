"""
Company boards from Common Crawl's URL index. No network: the index is
answered here, in the shapes it returned on 2026-09-28
(`{"pages": 5, "pageSize": 5, "blocks": 24}`, then one `{"url": ...}` per line).
"""

import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.models.company_board import CompanyBoard
from app.models.profile import Profile
from app.services import commoncrawl, tunables

API = "https://index.commoncrawl.org/CC-MAIN-2026-39-index"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(commoncrawl.time, "sleep", lambda s: None)


def index(monkeypatch, *, crawl="CC-MAIN-2026-39", pages=2, fail_on=None, calls=None):
    """A fake index: every target has `pages` pages, each naming two boards."""
    def get(url, **kw):
        if calls is not None:
            calls.append(url)
        if url == commoncrawl.COLLINFO_URL:
            return httpx.Response(200, json=[{"id": crawl, "cdx-api": API}],
                                  request=httpx.Request("GET", url))
        qs = parse_qs(urlsplit(url).query)
        target = qs["url"][0]
        if "showNumPages" in qs:
            return httpx.Response(200, json={"pages": pages, "pageSize": 5},
                                  request=httpx.Request("GET", url))
        page = int(qs["page"][0])
        if fail_on and (target, page) == fail_on:
            raise httpx.ConnectError("Connection reset by peer")
        lines = []
        if target == "job-boards.greenhouse.io/*":
            lines = [f"https://job-boards.greenhouse.io/co{page}a/jobs/1",
                     f"https://job-boards.greenhouse.io/co{page}a/jobs/2",
                     f"https://job-boards.greenhouse.io/co{page}b/jobs/3?gh_src=x"]
        elif target == "myworkdayjobs.com":
            lines = [f"https://t{page}.wd5.myworkdayjobs.com/Ext/job/X_{page}"]
        body = "\n".join(json.dumps({"url": u}) for u in lines)
        return httpx.Response(200, text=body, request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx, "get", get)


def _profile(db, overrides=None, state=None):
    db.query(Profile).delete()
    data = {tunables.STORE_KEY: overrides or {}}
    if state is not None:
        data[commoncrawl.STATE_KEY] = state
    db.add(Profile(data=data))
    db.commit()
    return db.query(Profile).first()


def _state(db):
    return db.query(Profile).first().data[commoncrawl.STATE_KEY]


class TestAWalk:
    def test_boards_on_the_pages_read_go_to_the_registry_for_probing(self, db, monkeypatch):
        index(monkeypatch)
        _profile(db)
        report = commoncrawl.run(db, pages_per_run=2, force=True)
        assert report["pages"] == 2 and report["new_boards"] == 4
        boards = {b.slug: b for b in db.query(CompanyBoard).filter_by(ats="greenhouse")}
        assert set(boards) == {"co0a", "co0b", "co1a", "co1b"}
        # Validation is off in tests (conftest); with it on these would wait for
        # a probe. Either way they are filed as coming from the index.
        assert {b.origin for b in boards.values()} == {"commoncrawl"}

    def test_the_budget_holds_and_the_next_walk_resumes(self, db, monkeypatch):
        calls = []
        index(monkeypatch, calls=calls)
        _profile(db)
        commoncrawl.run(db, pages_per_run=3, force=True)
        state = _state(db)
        assert state["cursor"] == {"greenhouse-new": 2, "greenhouse": 1}
        calls.clear()
        commoncrawl.run(db, pages_per_run=1, force=True)
        pages_read = [c for c in calls if "&page=" in c]
        assert len(pages_read) == 1 and "boards.greenhouse.io" in pages_read[0]
        assert _state(db)["cursor"]["greenhouse"] == 2

    def test_workday_tenants_are_found_across_subdomains(self, db, monkeypatch):
        index(monkeypatch, pages=1)
        _profile(db)
        commoncrawl.run(db, pages_per_run=len(commoncrawl.TARGETS), force=True)
        assert db.query(CompanyBoard).filter_by(ats="workday", slug="t0:wd5:Ext").count() == 1

    def test_a_finished_walk_says_so(self, db, monkeypatch):
        index(monkeypatch, pages=1)
        _profile(db)
        report = commoncrawl.run(db, pages_per_run=100, force=True)
        assert report["complete"] and _state(db)["complete"]

    def test_a_new_crawl_starts_the_walk_again(self, db, monkeypatch):
        index(monkeypatch, crawl="CC-MAIN-2026-43")
        _profile(db, state={"crawl": "CC-MAIN-2026-39", "cursor": {"greenhouse-new": 2},
                            "pages": {"greenhouse-new": 2}})
        commoncrawl.run(db, pages_per_run=1, force=True)
        state = _state(db)
        assert state["crawl"] == "CC-MAIN-2026-43"
        assert state["cursor"] == {"greenhouse-new": 1}

    def test_a_failing_page_keeps_its_cursor(self, db, monkeypatch):
        index(monkeypatch, fail_on=("job-boards.greenhouse.io/*", 1))
        _profile(db)
        report = commoncrawl.run(db, pages_per_run=5, force=True)
        assert report["pages"] == 1 and "greenhouse-new" in report["stopped"]
        state = _state(db)
        assert state["cursor"]["greenhouse-new"] == 1 and not state["complete"]
        # What the page before it found is kept.
        assert db.query(CompanyBoard).filter_by(slug="co0a").count() == 1

    def test_other_profile_keys_survive_the_write(self, db, monkeypatch):
        index(monkeypatch, pages=1)
        profile = _profile(db)
        profile.data = {**profile.data, "agents": {"laptop": {"harvest_sites": ["x.com"]}}}
        db.commit()
        commoncrawl.run(db, pages_per_run=1, force=True)
        assert db.query(Profile).first().data["agents"] == {"laptop": {"harvest_sites": ["x.com"]}}


class TestWhenItRuns:
    def test_not_due_is_one_cheap_answer(self, db, monkeypatch):
        calls = []
        index(monkeypatch, calls=calls)
        from datetime import datetime, timezone
        _profile(db, state={"last_run": datetime.now(timezone.utc).isoformat()})
        report = commoncrawl.run(db, interval_hours=24)
        assert report["skipped"] and calls == []

    def test_the_task_follows_the_settings_page(self, db, monkeypatch):
        from app.tasks import discovery

        monkeypatch.setattr(discovery, "SessionLocal", lambda: _Borrowed(db))
        calls = []
        index(monkeypatch, calls=calls)
        _profile(db, {"commoncrawl_enabled": False})
        assert discovery.discover_boards()["detail"] == "switched off"
        assert calls == []
        _profile(db, {"commoncrawl_enabled": True, "commoncrawl_pages_per_run": 1})
        report = discovery.discover_boards()
        assert report["pages"] == 1
        _profile(db, {"commoncrawl_enabled": True, "commoncrawl_pages_per_run": 3})
        assert discovery.discover_boards()["pages"] == 3


class _Borrowed:
    """The test session, handed to the task without it closing it."""

    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    def close(self):
        pass


def test_a_reset_connection_is_retried(monkeypatch):
    attempts = []

    def get(url, **kw):
        attempts.append(url)
        if len(attempts) < 3:
            raise httpx.ConnectError("Connection reset by peer")
        return httpx.Response(200, json=[{"id": "C", "cdx-api": API}],
                              request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx, "get", get)
    assert commoncrawl.latest_crawl() == ("C", API)
    assert len(attempts) == 3
