"""
What each source finds alone, and how much of SimplifyJobs' list our own
readers reach — from `jobs.seen_by`, with no request going out.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.models.job import Job, JobStatus
from app.models.profile import Profile
from app.services import job_fetcher, source_yield, tunables
from app.services.deduplication import note_source
from tests.test_fetch_task import _make_profile_with_targets, _std_job

NOW = datetime.now(timezone.utc)
LEVER = "https://jobs.lever.co/acme-robotics/5cde0d09-ba2d-408d-947e-4a42028cd4f7"


def job(db, *, seen_by, title="Software Engineer", status=JobStatus.new, days_ago=1,
        url=None, closed=False):
    row = Job(source=seen_by[0] if seen_by else "simplify", seen_by=seen_by,
              source_urls=[], title=title, company="Acme", location="Remote",
              url=url or f"https://x.example/{uuid.uuid4()}", status=status,
              fetched_at=NOW - timedelta(days=days_ago), dedupe_hash=uuid.uuid4().hex,
              closed_at=NOW if closed else None)
    row.source_urls = [row.url]
    db.add(row)
    db.commit()
    return row


# --- Recording who listed a job ------------------------------------------------

def cycle(db, jobs):
    with patch("app.services.query_expansion.expand_search_queries",
               return_value=(["Software Engineer"], None)), \
         patch("app.services.job_fetcher._run_all_adapters",
               side_effect=lambda *a, **k: (list(jobs), {})):
        return job_fetcher.fetch_and_save_jobs(db)


class TestSeenBy:
    def test_every_source_that_lists_a_job_is_recorded_once(self, db):
        _make_profile_with_targets(db)
        simplify = _std_job(source="simplify", source_job_id="s-1", company="Acme Robotics",
                            title="Software Engineer New Grad", url=LEVER + "/apply")
        lever = _std_job(source="lever", source_job_id="5cde0d09-ba2d-408d-947e-4a42028cd4f7",
                         company="acme-robotics", title="New Grads 2027 - Software Engineer",
                         url=LEVER)
        cycle(db, [simplify])
        cycle(db, [lever, simplify])
        assert db.query(Job).one().seen_by == ["simplify", "lever"]

    def test_a_row_from_before_counts_its_own_source(self):
        old = Job(source="greenhouse", seen_by=None)
        note_source(old, "simplify")
        assert old.seen_by == ["greenhouse", "simplify"]
        note_source(old, "greenhouse")
        assert old.seen_by == ["greenhouse", "simplify"]


# --- What each source found alone ----------------------------------------------

def test_per_source_counts_what_only_it_found(db):
    job(db, seen_by=["simplify", "lever"], status=JobStatus.matched)
    job(db, seen_by=["lever"], status=JobStatus.matched)
    job(db, seen_by=["lever"])
    job(db, seen_by=["simplify"])
    job(db, seen_by=["jazzhr"], days_ago=40)              # before the window
    by = {r["source"]: r for r in source_yield.per_source(db, 30)}
    assert by["lever"] == {"source": "lever", "seen": 3, "only_here": 2,
                           "matched": 2, "matched_only_here": 1}
    assert by["simplify"]["only_here"] == 1 and "jazzhr" not in by


def test_a_row_without_seen_by_counts_as_its_source(db):
    row = job(db, seen_by=None)
    row.source = "greenhouse"
    db.commit()
    assert source_yield.per_source(db, 30) == [
        {"source": "greenhouse", "seen": 1, "only_here": 1, "matched": 0, "matched_only_here": 0}]


# --- Recall against SimplifyJobs -----------------------------------------------------

def test_recall_is_the_share_of_the_list_another_source_found(db):
    job(db, seen_by=["simplify", "lever"], url=LEVER + "/apply")
    job(db, seen_by=["lever", "simplify"], url=LEVER.replace("5cde", "6cde"))
    job(db, seen_by=["simplify"], url="https://acme.wd5.myworkdayjobs.com/Ext/job/NYC/SWE_R1")
    job(db, seen_by=["simplify"], title="Accountant",
        url="https://boards.greenhouse.io/acme/jobs/123")
    job(db, seen_by=["simplify"], url="https://acme-careers.example/jobs/9")
    job(db, seen_by=["simplify"], closed=True)                 # closed: not counted
    job(db, seen_by=["lever"])                                 # not on the list
    found = source_yield.recall(db, 30, ["Software Engineer"])
    assert (found["total"], found["found"]) == (5, 2)
    assert (found["roles_total"], found["roles_found"]) == (4, 2)
    assert found["recognised"] == 4
    assert dict(found["missed"]) == {"workday": 1, "acme-careers.example": 1}


class TestTheSettingsPageControlsIt:
    def test_the_window(self, db):
        job(db, seen_by=["simplify", "lever"], days_ago=2)
        job(db, seen_by=["simplify"], days_ago=20)
        wide = source_yield.measure(db, {"target_roles": ["Software Engineer"]})["latest"]
        narrow = source_yield.measure(db, {"target_roles": ["Software Engineer"],
                                           tunables.STORE_KEY: {"recall_window_days": 7}})["latest"]
        assert (wide["total"], wide["roles_share"]) == (2, 50.0)
        assert (narrow["total"], narrow["roles_share"], narrow["window_days"]) == (1, 100.0, 7)


def test_the_trend_keeps_one_entry_a_day(db):
    job(db, seen_by=["simplify", "lever"])
    first = source_yield.measure(db, {"target_roles": ["Software Engineer"]})
    history = [{"date": "2026-09-01", "total": 9, "found": 3, "roles_total": 9,
                "roles_found": 3, "roles_share": 33.3, "share": 33.3}] + first["history"]
    again = source_yield.measure(db, {"target_roles": ["Software Engineer"],
                                      source_yield.STATE_KEY: {"history": history}})
    assert [h["date"] for h in again["history"]] == ["2026-09-01", first["latest"]["date"]]


# --- On the runs page --------------------------------------------------------------

class TestTheRunsPage:
    def test_before_any_measurement(self, client, db):
        db.add(Profile(data={"target_roles": ["Software Engineer"]}))
        db.commit()
        assert "Not measured yet" in client.get("/runs").text

    def test_measure_now(self, client, db):
        db.add(Profile(data={"target_roles": ["Software Engineer"]}))
        db.commit()
        job(db, seen_by=["simplify", "lever"])
        job(db, seen_by=["simplify"], url="https://acme.wd5.myworkdayjobs.com/Ext/job/NYC/SWE_R1")
        body = client.post("/runs/coverage").text
        assert "50.0%" in body and "workday" in body and "Only here" in body
        stored = db.query(Profile).first().data[source_yield.STATE_KEY]
        assert stored["latest"]["roles_found"] == 1
        assert "50.0%" in client.get("/runs").text
