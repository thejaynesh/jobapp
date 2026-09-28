"""
A matching pass with several jobs' model calls in flight
(`matcher._match_concurrently`): the calls overlap, the database work stays on
the caller's thread in the batch's order, and starts are still paced. The
model is faked at `llm_score_job`; nothing leaves the process.
"""

import threading
import time
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.models.job import Job, JobStatus
from app.models.profile import Profile
from app.services import matcher, tunables

PROFILE = {"target_roles": ["Backend Engineer"], "skills": {"lang": ["Python", "Go"]}}
LONG = "We need a backend engineer with Python and Go. " * 20
REPLY = {"score": 88, "reasoning": "fits", "matched_skills": ["Python"],
         "missing_skills": [], "seniority_fit": True, "scored_by": "nim/test"}


def make_jobs(db, n, **overrides):
    jobs = []
    for i in range(n):
        fields = dict(source="greenhouse", title="Backend Engineer", company=f"Acme {i}",
                      location="Remote", url=f"https://x.example/{uuid.uuid4()}",
                      description=LONG, status=JobStatus.new,
                      fetched_at=datetime.now(timezone.utc), dedupe_hash=uuid.uuid4().hex,
                      details_extracted_at=datetime.now(timezone.utc))
        fields.update(overrides)
        job = Job(source_urls=[fields["url"]], **fields)
        db.add(job)
        jobs.append(job)
    db.commit()
    return jobs


def set_profile(db, overrides):
    db.query(Profile).delete()
    db.add(Profile(data={**PROFILE, tunables.STORE_KEY: overrides}))
    db.commit()


@pytest.fixture(autouse=True)
def _no_second_opinion():
    with patch("app.llm.providers.deep_matching_chain", return_value=[]), \
         patch.object(matcher, "match_pace_seconds", return_value=0.0):
        yield


class TestTheSettingsPageControlsIt:
    def test_several_jobs_are_scored_at_once(self, db):
        set_profile(db, {"match_concurrency": 3})
        make_jobs(db, 3)
        barrier = threading.Barrier(3, timeout=5)

        def score(*args, **kwargs):
            barrier.wait()          # only passes when three calls are in flight
            return dict(REPLY)

        with patch.object(matcher, "llm_score_job", side_effect=score):
            result = matcher.match_all_new_jobs(db, budget={"paid_calls": 0})
        assert result["matched"] == 3 and result["errors"] == 0

    def test_one_scores_them_one_at_a_time(self, db):
        set_profile(db, {"match_concurrency": 1})
        make_jobs(db, 2)
        barrier = threading.Barrier(2, timeout=0.3)

        def score(*args, **kwargs):
            barrier.wait()
            return dict(REPLY)

        with patch.object(matcher, "llm_score_job", side_effect=score):
            result = matcher.match_all_new_jobs(db, budget={"paid_calls": 0})
        assert result["matched"] == 0 and result["errors"] == 2


def test_jobs_are_filed_in_the_batchs_order(db):
    set_profile(db, {"match_concurrency": 4})
    jobs = make_jobs(db, 4)
    order = [j.id for j in sorted(jobs, key=lambda j: j.fetched_at, reverse=True)]
    delays = {jid: 0.3 - 0.07 * i for i, jid in enumerate(order)}   # first is slowest

    def score(job, *args, **kwargs):
        time.sleep(delays[job.id])
        return dict(REPLY)

    filed = []
    with patch.object(matcher, "llm_score_job", side_effect=score):
        matcher.match_all_new_jobs(db, budget={"paid_calls": 0},
                                   on_matched=lambda job: filed.append(job.id))
    assert filed == order
    assert all(db.get(Job, jid).status == JobStatus.matched for jid in order)


def test_a_job_the_filter_rejects_is_filed_without_a_call(db):
    set_profile(db, {"match_concurrency": 3})
    make_jobs(db, 2)
    make_jobs(db, 1, title="Dental Hygienist")
    with patch.object(matcher, "llm_score_job", return_value=dict(REPLY)) as score:
        result = matcher.match_all_new_jobs(db, budget={"paid_calls": 0})
    assert score.call_count == 2
    assert (result["matched"], result["filtered_out"]) == (2, 1)


def test_details_read_on_a_snapshot_reach_the_job(db):
    set_profile(db, {"match_concurrency": 2})
    jobs = make_jobs(db, 2, details_extracted_at=None)

    def extract(target):
        assert not isinstance(target, Job)      # evaluated off the session
        target.required_years = 3
        target.language = "en"
        target.details_extracted_at = datetime.now(timezone.utc)
        return True

    with patch("app.services.job_details.extract_and_apply", side_effect=extract), \
         patch.object(matcher, "llm_score_job", return_value=dict(REPLY)):
        matcher.match_all_new_jobs(db, budget={"paid_calls": 0})
    for job in jobs:
        db.refresh(job)
        assert job.required_years == 3 and job.details_extracted_at is not None
        assert job.matched_by == "nim/test"


def test_a_rate_limited_job_stays_new(db):
    set_profile(db, {"match_concurrency": 2})
    jobs = make_jobs(db, 2)
    with patch.object(matcher, "llm_score_job", side_effect=matcher.LLMUnavailableError("down")):
        result = matcher.match_all_new_jobs(db, budget={"paid_calls": 0})
    assert result["rate_limited"] == 2
    assert all(db.get(Job, j.id).status == JobStatus.new for j in jobs)


def test_the_pacer_spaces_starts_across_threads():
    pacer = matcher._Pacer(0.1)
    starts = []

    def go():
        pacer.wait()
        starts.append(time.monotonic())

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    starts.sort()
    assert all(b - a >= 0.09 for a, b in zip(starts, starts[1:]))


def test_no_call_is_lost_from_the_budget_across_threads():
    # The budget is the cap on paid calls; a count lost to a race is a call
    # the cap never saw. Switching threads as often as possible makes the
    # unlocked read-then-write lose counts reliably.
    import sys

    budget = {"paid_calls": 0}
    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        threads = [threading.Thread(target=lambda: [matcher._spend(budget, "paid_calls")
                                                    for _ in range(20_000)])
                   for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.setswitchinterval(interval)
    assert budget["paid_calls"] == 8 * 20_000
