"""Regressions from the September source-intake production report."""

import html
import json
from datetime import datetime, timedelta, timezone
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.company_board import CompanyBoard
from app.services import company_boards, fetch_lock
from app.services.ats_discovery import build_ats_slugs
from app.services.sources.listing_fallbacks import extract_listing_jobs


@pytest.fixture
def board_db():
    # These selection/probe queries need only the board table. SQLite keeps
    # this bounded regression suite independent of a running Postgres server.
    engine = create_engine("sqlite://")
    CompanyBoard.__table__.create(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def board(db, slug, ats="greenhouse", **kwargs):
    now = datetime.now(timezone.utc)
    values = dict(active=True, origin="discovered", first_seen_at=now,
                  last_seen_at=now, validated_at=now,
                  last_job_count=0, total_job_count=0, consecutive_empty=0)
    values.update(kwargs)
    row = CompanyBoard(ats=ats, slug=slug, **values)
    db.add(row)
    db.flush()
    return row


def test_rotation_reaches_every_unpolled_board_without_growing_budget(board_db):
    now = datetime.now(timezone.utc)
    for i in range(8):
        board(board_db, f"busy{i}", last_job_count=100, total_job_count=1000,
              last_fetched_at=now)
    for i in range(16):
        board(board_db, f"untried{i:02}")
    covered = set()
    for cycle in range(8):
        slugs = company_boards.board_slugs(board_db, "greenhouse", 8)
        assert len(slugs) == len(set(slugs)) == 8
        assert len([s for s in slugs if s.startswith("busy")]) == 6
        covered.update(slugs)
        for row in board_db.query(CompanyBoard).filter(CompanyBoard.slug.in_(slugs)):
            row.last_fetched_at = now + timedelta(hours=cycle + 1)
        board_db.flush()
    assert {f"untried{i:02}" for i in range(16)} <= covered


def test_rotation_excludes_inactive_and_handles_zero_budget(board_db):
    board(board_db, "retired", active=False)
    board(board_db, "active")
    assert company_boards.board_slugs(board_db, "greenhouse", 0) == []
    assert company_boards.board_slugs(board_db, "greenhouse", 1) == ["active"]


def test_oldest_polled_board_gets_a_rotation_slot(board_db):
    now = datetime.now(timezone.utc)
    board(board_db, "old", last_fetched_at=now - timedelta(days=30))
    for i in range(5):
        board(board_db, f"busy{i}", last_job_count=100, last_fetched_at=now)
    assert company_boards.board_slugs(board_db, "greenhouse", 4)[0] == "old"


def test_registry_does_not_reintroduce_seeds_or_rejected_legacy_slugs():
    cfg = SimpleNamespace(ATS_SEED_COMPANIES=True, GREENHOUSE_COMPANY_SLUGS="chosen")
    selected = build_ats_slugs(cfg, discovered={"greenhouse": ["rejected"]},
                               registry={"greenhouse": ["new-board", "producer"]})
    assert selected["greenhouse"] == ["chosen", "new-board", "producer"]
    assert selected["workday"] == []
    assert build_ats_slugs(SimpleNamespace(ATS_SEED_COMPANIES=True), registry={})["greenhouse"] == []
    assert "stripe" in build_ats_slugs(SimpleNamespace(ATS_SEED_COMPANIES=True))["greenhouse"]


TEAMTAILOR = '''<ul><li><div><a href="/jobs/123456-backend">
  <span></span>Backend &amp; API Engineer</a><span><span>Engineering</span>
  <span>·</span><span>Canada, USA</span><span>·</span><span>Fully Remote</span>
  </span></div></li></ul>'''
JOBVITE = '''<table><tr><td class="jv-job-list-name">
  <a href="/acme/job/oABC123">Backend Engineer</a></td>
  <td class="jv-job-list-location"><div>New York, NY</div></td></tr></table>'''


@pytest.mark.parametrize("source,page,url,location", [
    ("teamtailor", TEAMTAILOR, "https://acme.teamtailor.com/jobs", "Canada, USA; Fully Remote"),
    ("jobvite", JOBVITE, "https://jobs.jobvite.com/acme/search", "New York, NY"),
])
def test_public_cards_are_jobs_without_fabricated_descriptions(source, page, url, location):
    jobs = extract_listing_jobs(page, url, source, "acme")
    assert len(jobs) == 1
    assert jobs[0]["location"] == location
    assert jobs[0]["source_job_id"].startswith("acme:")
    assert jobs[0]["description"] == ""
    assert jobs[0]["posted_at"] is None
    assert jobs[0]["is_remote"] == (source == "teamtailor")


def test_cards_ignore_external_links_and_wrong_tenant():
    page = JOBVITE.replace('/acme/job/', '/other/job/') + TEAMTAILOR.replace(
        '/jobs/123456-backend', 'https://unrelated.example/jobs/123456-backend')
    assert extract_listing_jobs(page, "https://jobs.jobvite.com/acme", "jobvite", "acme") == []
    assert extract_listing_jobs(page, "https://acme.teamtailor.com/jobs", "teamtailor", "acme") == []


def test_yc_reads_embedded_page_data_and_rejects_incomplete_rows():
    good = dict(id=42, title="Software Engineer", companyName="Example",
                url="/companies/example/jobs/abc-software-engineer", location="Remote",
                createdAt="21 days")
    payload = {"component": "WaasJobListingsPage", "props": {"jobPostings": [
        good, good, {**good, "companyName": None},
        {**good, "url": "https://other.example/companies/example/jobs/1"},
        {**good, "url": "/jobs/role/software-engineer"},
    ]}}
    page = '<div data-page="' + html.escape(json.dumps(payload), quote=True) + '"></div>'
    jobs = extract_listing_jobs(page, "https://www.ycombinator.com/jobs/role/software-engineer",
                                "ycombinator", "software-engineer")
    assert len(jobs) == 1
    assert jobs[0]["company"] == "Example"
    assert jobs[0]["source_job_id"] == "42"
    assert jobs[0]["url"].startswith("https://www.ycombinator.com/companies/")
    assert jobs[0]["posted_at"] is None


@pytest.mark.parametrize("raw", ["not json", "[]", '{"component":"Other"}',
    '{"component":"WaasJobListingsPage","props":{"jobPostings":null}}'])
def test_malformed_yc_data_is_not_a_job(raw):
    page = '<div data-page="' + html.escape(raw, quote=True) + '"></div>'
    assert extract_listing_jobs(page, "https://www.ycombinator.com", "ycombinator", "role") == []


@pytest.mark.parametrize("source,page,url", [
    ("teamtailor", TEAMTAILOR, "https://acme.teamtailor.com/jobs"),
    ("jobvite", JOBVITE, "https://jobs.jobvite.com/acme/search"),
])
def test_fetcher_and_validator_agree_on_card_format(monkeypatch, source, page, url):
    from app.services.sources.base import jobs_from_listing
    from app.services.ats_validation import probe_board

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: httpx.Response(
        200, text=page, request=httpx.Request("GET", url)))
    assert probe_board(source, "acme").exists
    assert len(jobs_from_listing(url, source, "acme")) == 1


@pytest.mark.parametrize("ats", ["jobvite", "teamtailor", "icims"])
def test_old_false_rejections_are_reprobed_once_within_limit(board_db, monkeypatch, ats):
    from app.services.ats_validation import BoardProbe

    rows = [board(board_db, str(i), ats=ats, active=False,
                  inactive_reason=f"{ats} has no board for this slug") for i in range(3)]
    board(board_db, "other", ats="personio", active=False,
          inactive_reason="personio has no board for this slug")
    probe = Mock(return_value=BoardProbe(False, error=f"{ats}: no readable job listing"))
    monkeypatch.setattr("app.services.ats_validation.probe_board", probe)
    assert company_boards.validate_pending(board_db, limit=2)["probed"] == 2
    assert company_boards.validate_pending(board_db, limit=2)["probed"] == 1
    assert company_boards.validate_pending(board_db, limit=2)["probed"] == 0
    assert all(not row.active for row in rows)


@pytest.mark.parametrize("ats", ["teamtailor", "jobvite", "icims"])
def test_old_false_rejection_can_be_activated(board_db, monkeypatch, ats):
    from app.services.ats_validation import BoardProbe

    row = board(board_db, "acme", ats=ats, active=False,
                inactive_reason=f"{ats} has no board for this slug")
    monkeypatch.setattr("app.services.ats_validation.probe_board",
                        lambda *a: BoardProbe(True))
    assert company_boards.validate_pending(board_db)["activated"] == 1
    assert row.active and row.inactive_reason is None


@pytest.mark.parametrize("lost", [False, True])
def test_fetch_lock_heartbeat_uses_original_ownership_and_stops(monkeypatch, lost):
    # The clock fires immediately once; subsequent waits stop on context exit.
    ticked, finished = Event(), Event()

    class Clock:
        first = True

        def wait(self, timeout):
            if self.first:
                self.first = False
                if lost:
                    fetch_lock._held_tokens["test-fetch"] = "replacement"
                return False
            return finished.wait(timeout)

        def is_set(self):
            return finished.is_set()

        def set(self):
            finished.set()

    calls = []

    def renew(script, numkeys, key, token, ttl):
        calls.append((script, key, token, ttl))
        ticked.set()
        return 0 if lost else 1

    monkeypatch.setattr(fetch_lock, "Event", Clock)
    monkeypatch.setattr(fetch_lock, "_held_tokens", {"test-fetch": "owner"})
    monkeypatch.setattr(fetch_lock, "_client", lambda: SimpleNamespace(eval=renew))
    with pytest.raises(RuntimeError):
        with fetch_lock.keepalive(["test-fetch"]):
            assert ticked.wait(2)
            raise RuntimeError("cycle failed")
    assert finished.is_set()
    assert len(calls) == 1
    assert calls[0][1:] == ("test-fetch", "owner", 1800)
    assert "redis.call('get', KEYS[1]) == ARGV[1]" in calls[0][0]


@pytest.mark.parametrize("ats", ["teamtailor", "jobvite", "icims"])
def test_an_unreachable_recheck_leaves_a_rejected_board_rejected(board_db, monkeypatch, ats):
    """
    A probe that errors reads as "exists" so that new boards fail open. On a
    board already found dead, that turned it back on over a network blip.
    """
    from app.services.ats_validation import BoardProbe

    reason = f"{ats} has no board for this slug"
    row = board(board_db, "acme", ats=ats, active=False, inactive_reason=reason)
    monkeypatch.setattr("app.services.ats_validation.probe_board",
                        lambda *a: BoardProbe(True, error="probe failed: timed out"))
    counts = company_boards.validate_pending(board_db)
    assert counts["activated"] == 0 and counts["unreachable"] == 1
    assert not row.active and row.inactive_reason == reason


def test_a_new_board_still_fails_open_when_unreachable(board_db, monkeypatch):
    from app.services.ats_validation import BoardProbe

    row = board(board_db, "fresh", active=False, validated_at=None,
                inactive_reason="awaiting validation")
    monkeypatch.setattr("app.services.ats_validation.probe_board",
                        lambda *a: BoardProbe(True, error="probe failed: timed out"))
    company_boards.validate_pending(board_db)
    assert row.active
