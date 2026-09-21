"""
Dead-posting detection.

The rule under test everywhere: only certainty closes a job. A 404, a page
that says outright the role is gone, or a known ATS bouncing the job URL back
to its board index — anything else (a timeout, a bot-check, a 403) must leave
the posting alone, because "couldn't tell" shown as "closed" buries live jobs.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.models.job import Job, JobStatus
from app.services import liveness

_NOW = datetime.now(timezone.utc)


class _FakeResponse:
    def __init__(self, status_code=200, text="", url=None,
                 content_type="text/html; charset=utf-8"):
        self.status_code = status_code
        self.text = text
        self.url = url
        self.headers = {"content-type": content_type}


def _client_returning(response):
    client = MagicMock()
    client.get.return_value = response
    return client


class TestCheckUrl:
    URL = "https://boards.example.com/acme/jobs/123"

    def test_a_404_closes_the_posting(self):
        result = liveness.check_url(
            self.URL, _client_returning(_FakeResponse(404, url=self.URL)))
        assert result.state == "closed"
        assert "404" in result.note

    def test_a_gone_closes_the_posting(self):
        result = liveness.check_url(
            self.URL, _client_returning(_FakeResponse(410, url=self.URL)))
        assert result.state == "closed"

    def test_a_403_is_ambiguous_not_closed(self):
        # A bot-check says something about us, not about the job.
        result = liveness.check_url(
            self.URL, _client_returning(_FakeResponse(403, url=self.URL)))
        assert result.state == "unknown"

    def test_an_unreachable_site_is_unknown(self):
        client = MagicMock()
        client.get.side_effect = RuntimeError("connection refused")
        result = liveness.check_url(self.URL, client)
        assert result.state == "unknown"

    def test_a_closed_banner_closes_the_posting(self):
        page = "<html><body><h1>Sorry!</h1> This job is no longer available.</body></html>"
        result = liveness.check_url(
            self.URL, _client_returning(_FakeResponse(200, text=page, url=self.URL)))
        assert result.state == "closed"
        assert "no longer available" in result.note

    def test_an_ordinary_posting_stays_open(self):
        page = "<html><body>Backend Engineer. Apply now!</body></html>"
        result = liveness.check_url(
            self.URL, _client_returning(_FakeResponse(200, text=page, url=self.URL)))
        assert result.state == "open"

    def test_an_ats_redirect_to_the_board_index_closes_it(self):
        url = "https://boards.greenhouse.io/acme/jobs/4567890"
        landed = _FakeResponse(200, text="Open roles at Acme",
                               url="https://boards.greenhouse.io/acme")
        result = liveness.check_url(url, _client_returning(landed))
        assert result.state == "closed"

    def test_the_same_redirect_on_an_unknown_host_does_not(self):
        # An arbitrary employer site redirecting could be a plain URL change.
        url = "https://careers.acme.com/openings/backend-engineer-4567890"
        landed = _FakeResponse(200, text="Careers at Acme",
                               url="https://careers.acme.com/")
        result = liveness.check_url(url, _client_returning(landed))
        assert result.state == "open"


def _make_job(db, suffix, *, status=JobStatus.docs_generated, checked_at=None,
              closed_at=None):
    job = Job(
        source="adzuna",
        title=f"Backend Engineer {suffix}",
        company="Acme",
        location="NYC",
        url=f"https://ex.com/live/{suffix}",
        description="A job.",
        experience_level="mid",
        status=status,
        fetched_at=_NOW,
        dedupe_hash=f"live-{suffix}",
        liveness_checked_at=checked_at,
        closed_at=closed_at,
    )
    db.add(job)
    db.flush()
    return job


class TestCandidates:
    def test_only_jobs_worth_applying_to_are_checked(self, db):
        wanted = _make_job(db, "c1")
        _make_job(db, "c2", status=JobStatus.filtered_out)
        _make_job(db, "c3", status=JobStatus.new)
        ids = {job.id for job in liveness.candidates(db, limit=10, recheck_days=3)}
        assert wanted.id in ids
        assert len(ids) == 1

    def test_a_recent_verdict_stands(self, db):
        _make_job(db, "c4", checked_at=_NOW - timedelta(hours=1))
        assert liveness.candidates(db, limit=10, recheck_days=3) == []

    def test_a_stale_verdict_is_rechecked(self, db):
        job = _make_job(db, "c5", checked_at=_NOW - timedelta(days=10))
        ids = {j.id for j in liveness.candidates(db, limit=10, recheck_days=3)}
        assert job.id in ids

    def test_a_known_closed_job_is_left_alone(self, db):
        _make_job(db, "c6", closed_at=_NOW)
        assert liveness.candidates(db, limit=10, recheck_days=3) == []


class TestSweep:
    def test_outcomes_are_recorded_per_job(self, db, monkeypatch):
        dead = _make_job(db, "s1")
        alive = _make_job(db, "s2")

        def fake_check(url, client):
            if "s1" in url:
                return liveness.LivenessResult("closed", "HTTP 404")
            return liveness.LivenessResult("open")

        monkeypatch.setattr(liveness, "check_url", fake_check)
        counts = liveness.sweep(db, limit=10, workers=1)

        # Still exact on the outcome counters — the four of them have to sum to
        # `checked`, and a fifth appearing among them would mean a job got an
        # outcome nobody accounted for. Coverage is a separate key because it
        # reports on the schedule, not on this run.
        outcomes = {k: v for k, v in counts.items() if k != "coverage"}
        assert outcomes == {"checked": 2, "closed": 1, "still_open": 1,
                            "unknown": 0}
        assert dead.closed_at is not None
        assert "404" in dead.closed_note
        assert alive.closed_at is None
        assert alive.liveness_checked_at is not None

    def test_an_ambiguous_answer_only_updates_the_clock(self, db, monkeypatch):
        job = _make_job(db, "s3")
        monkeypatch.setattr(
            liveness, "check_url",
            lambda url, client: liveness.LivenessResult("unknown", "HTTP 403"),
        )
        counts = liveness.sweep(db, limit=10, workers=1)
        assert counts["unknown"] == 1
        assert job.closed_at is None
        assert job.liveness_checked_at is not None

    def test_an_empty_queue_is_a_quiet_no_op(self, db):
        result = liveness.sweep(db, limit=10, workers=1)
        outcomes = {k: v for k, v in result.items() if k != "coverage"}

        assert outcomes == {
            "checked": 0, "closed": 0, "still_open": 0, "unknown": 0,
        }
        # Present even on a run that did nothing, so a caller can read it
        # without first checking whether the sweep found work.
        assert "coverage" in result


class TestWhetherTheBudgetCanKeepUp:
    """
    The schedule's arithmetic, said out loud.

    `LIVENESS_MAX_PER_CYCLE` per sweep x sweeps per day x `RECHECK_DAYS` is the
    number of jobs the configuration can keep fresh. Above it nothing errors —
    `candidates` orders never-checked first, so the oldest verdicts simply stop
    being reached and keep showing a months-old "still open". A numerator with
    no denominator, which was the complaint.
    """

    def test_the_defaults_multiply_out(self, db, monkeypatch):
        monkeypatch.setattr(settings, "LIVENESS_MAX_PER_CYCLE", 200)
        monkeypatch.setattr(settings, "LIVENESS_INTERVAL_HOURS", 12)
        monkeypatch.setattr(settings, "LIVENESS_RECHECK_DAYS", 3)

        out = liveness.coverage(db)

        assert out["daily_budget"] == 400    # 200 x (24/12)
        assert out["sustains"] == 1200       # x 3 days

    def test_a_backlog_past_the_budget_is_reported_unsustainable(
            self, db, monkeypatch):
        monkeypatch.setattr(settings, "LIVENESS_MAX_PER_CYCLE", 1)
        monkeypatch.setattr(settings, "LIVENESS_INTERVAL_HOURS", 24)
        monkeypatch.setattr(settings, "LIVENESS_RECHECK_DAYS", 1)
        for n in range(3):
            _make_job(db, f"cov-{n}")

        out = liveness.coverage(db)

        assert out["worth_checking"] == 3
        assert out["sustains"] == 1
        assert out["sustainable"] is False

    def test_a_closed_posting_is_no_longer_worth_checking(self, db, monkeypatch):
        monkeypatch.setattr(settings, "LIVENESS_MAX_PER_CYCLE", 200)
        _make_job(db, "cov-open")
        _make_job(db, "cov-shut", closed_at=_NOW)

        assert liveness.coverage(db)["worth_checking"] == 1

    def test_a_job_the_sweep_never_looks_at_is_not_counted_against_it(
            self, db, monkeypatch):
        # `candidates` only ever considers matched and docs_generated, so
        # counting anything else would invent a backlog that does not exist.
        _make_job(db, "cov-new", status=JobStatus.new)
        _make_job(db, "cov-filtered", status=JobStatus.filtered_out)

        assert liveness.coverage(db)["worth_checking"] == 0
