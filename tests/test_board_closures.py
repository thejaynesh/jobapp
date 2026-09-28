"""
Postings that disappear from their board are closed on the next read of it.

The real Greenhouse adapter runs against a mocked board through the real save
path, cycle after cycle. No network.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest

from app.models.job import Job
from app.services import job_fetcher
from app.services.sources import base, greenhouse
from tests.test_fetch_task import _make_profile_with_targets, _std_job

NOW = datetime.now(timezone.utc)


def posting(n, age_days=1):
    return {"id": 4000 + n, "title": f"Software Engineer {n}",
            "absolute_url": f"https://job-boards.greenhouse.io/acme/jobs/{4000 + n}",
            "location": {"name": "New York, NY"},
            "first_published": (NOW - timedelta(days=age_days)).isoformat(),
            "content": "Build things in Python."}


class Board:
    """A Greenhouse board whose listing each test changes between cycles."""

    def __init__(self, monkeypatch):
        self.listing: list | None = []
        self.status = 200
        monkeypatch.setattr(httpx, "get", self.get)

    def get(self, url, **kw):
        request = httpx.Request("GET", url)
        if self.status != 200:
            return httpx.Response(self.status, text="down", request=request)
        return httpx.Response(200, json={"jobs": self.listing}, request=request)


def cycle(db, extra_jobs=()):
    def run(*args, **kwargs):
        jobs = greenhouse.fetch(["acme"], max_age_days=30)
        return jobs + list(extra_jobs), {"greenhouse": {"count": len(jobs), "errors": [],
                                                        "enabled": True}}

    with patch("app.services.query_expansion.expand_search_queries",
               return_value=(["Software Engineer"], None)), \
         patch("app.services.job_fetcher._run_all_adapters", side_effect=run):
        return job_fetcher.fetch_and_save_jobs(db)


def _job(db, n) -> Job:
    return db.query(Job).filter_by(source="greenhouse", source_job_id=str(4000 + n)).one()


@pytest.fixture
def board(db, monkeypatch):
    _make_profile_with_targets(db)
    return Board(monkeypatch)


def test_a_posting_gone_from_its_board_is_closed(db, board):
    board.listing = [posting(1), posting(2)]
    cycle(db)
    assert _job(db, 2).board == "greenhouse:acme" and _job(db, 2).closed_at is None

    board.listing = [posting(1)]
    counts = cycle(db)
    assert counts["closed"] == 1
    assert _job(db, 2).closed_at is not None
    assert _job(db, 2).closed_note == job_fetcher.VANISHED_NOTE
    assert _job(db, 1).closed_at is None


def test_one_listed_again_is_reopened(db, board):
    board.listing = [posting(1), posting(2)]
    cycle(db)
    board.listing = [posting(1)]
    cycle(db)
    board.listing = [posting(1), posting(2)]
    cycle(db)
    assert _job(db, 2).closed_at is None and _job(db, 2).closed_note is None


def test_one_too_old_to_keep_is_still_listed_and_stays_open(db, board):
    """The adapter drops it for age, but the board still lists it."""
    board.listing = [posting(1), posting(2)]
    cycle(db)
    board.listing = [posting(1), posting(2, age_days=90)]
    assert cycle(db)["closed"] == 0
    assert _job(db, 2).closed_at is None


@pytest.mark.parametrize("change", ["error", "empty"])
def test_a_failed_or_empty_read_closes_nothing(db, board, change):
    board.listing = [posting(1), posting(2)]
    cycle(db)
    if change == "error":
        board.status = 503
    else:
        board.listing = []
    assert cycle(db)["closed"] == 0
    assert _job(db, 1).closed_at is None and _job(db, 2).closed_at is None


def test_a_row_another_source_stored_is_never_closed_this_way(db, board):
    """Its id is the other source's; against the board's ids it would look gone."""
    first = _std_job(source="simplify", source_job_id="simplify-1",
                     title="Software Engineer 2", company="acme", location="New York, NY",
                     url="https://job-boards.greenhouse.io/acme/jobs/4002")
    board.listing = []
    cycle(db, extra_jobs=[first])
    board.listing = [posting(1), posting(2)]
    cycle(db)
    board.listing = [posting(1)]
    cycle(db)
    row = db.query(Job).filter_by(url="https://job-boards.greenhouse.io/acme/jobs/4002").one()
    assert row.source == "simplify" and row.board is None and row.closed_at is None


def test_only_full_feed_boards_are_trusted_for_this():
    assert "workday" not in job_fetcher.FULL_FEED_BOARDS
    assert "smartrecruiters" not in job_fetcher.FULL_FEED_BOARDS
    assert job_fetcher._board_key({"source": "workday", "ats_slug": "x:wd1:y",
                                   "source_job_id": "1"}) is None


def test_sightings_are_filed_only_for_boards_that_answered():
    def fetch_one(slug):
        if slug == "down":
            raise RuntimeError("boom")
        base.saw_postings(["1", "2", None, ""])
        return []

    with base.collect_board_sightings() as sightings:
        base.fetch_boards_concurrently(["up", "down"], fetch_one, "Greenhouse", 2)
    assert sightings == {("greenhouse", "up"): {"1", "2"}}
