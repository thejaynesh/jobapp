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


def test_single_match_scores_newly_extracted_salary_from_its_snapshot(db, monkeypatch):
    from app.services import job_details

    job = make_jobs(db, 1, details_extracted_at=None)[0]
    monkeypatch.setattr(matcher, "_similarity_scorer", lambda *args: None)
    monkeypatch.setattr(job_details, "needs_extraction", lambda job: True)

    def extract(snapshot):
        job_details.apply(snapshot, {"salary_min": 65, "salary_max": 85,
                                    "salary_currency": "USD", "salary_period": "hour"})
        return True

    def score(snapshot, *args, **kwargs):
        assert not isinstance(snapshot, Job)
        assert "Stated salary: $65–$85/hr" in matcher._stated_facts(snapshot)
        return dict(REPLY)

    monkeypatch.setattr(job_details, "extract_and_apply", extract)
    monkeypatch.setattr(matcher, "llm_score_job", score)
    assert matcher.match_job(db, job, PROFILE, "unused", "https://unused.invalid", "stub") == "matched"
    db.commit()
    assert job.salary_label == "$65–$85/hr"
    assert job.salary_annual_min == 135200


def test_single_match_does_not_commit_caller_changes_on_model_failure(db, monkeypatch):
    from app.services import job_details

    profile = Profile(data={"name": "Before"})
    db.add(profile)
    job = make_jobs(db, 1)[0]
    profile.data = {"name": "Pending edit"}
    monkeypatch.setattr(matcher, "_similarity_scorer", lambda *args: None)
    monkeypatch.setattr(job_details, "needs_extraction", lambda job: False)

    with patch.object(matcher, "llm_score_job", side_effect=RuntimeError("Unavailable")), \
         patch.object(db, "commit", wraps=db.commit) as commit:
        with pytest.raises(RuntimeError, match="Unavailable"):
            matcher.match_job(db, job, PROFILE, "unused", "https://unused.invalid", "stub")
        commit.assert_not_called()
    assert job.keyword_score is None
    db.rollback()
    assert profile.data == {"name": "Before"}
    assert job.status == JobStatus.new and job.llm_score is None


@pytest.mark.parametrize("intervening_edit", [False, True])
def test_pending_requeue_is_distinct_from_a_committed_concurrent_edit(monkeypatch, intervening_edit):
    from sqlalchemy import text

    from tests.conftest import TestSessionLocal
    from app.services import job_details

    job_id = None
    monkeypatch.setattr(matcher, "_similarity_scorer", lambda *args: None)
    monkeypatch.setattr(job_details, "needs_extraction", lambda job: False)
    try:
        with TestSessionLocal() as setup:
            job_id = make_jobs(setup, 1, status=JobStatus.filtered_out,
                               filter_reason="low_score", llm_score=20)[0].id

        def score(snapshot, *args, **kwargs):
            assert snapshot.status == JobStatus.new
            with TestSessionLocal() as editing:
                editing.execute(text("SET LOCAL lock_timeout = '2s'"))
                current = editing.query(Job).filter(Job.id == job_id).with_for_update().one()
                # The caller's requeue has neither been flushed nor committed,
                # and it must not hold a lock across the model call.
                assert current.status == JobStatus.filtered_out
                if intervening_edit:
                    current.description = LONG * 3
                editing.commit()
            return dict(REPLY)

        with TestSessionLocal() as matching:
            job = matching.get(Job, job_id)
            job.status = JobStatus.new
            with patch.object(matcher, "llm_score_job", side_effect=score):
                outcome = matcher.match_job(matching, job, PROFILE,
                    "unused", "https://unused.invalid", "stub")
            assert outcome == ("superseded" if intervening_edit else "matched")
            matching.commit()

        with TestSessionLocal() as verifying:
            job = verifying.get(Job, job_id)
            if intervening_edit:
                assert job.description == LONG * 3
                assert job.llm_score == 20 and job.scores == []
            else:
                assert job.status == JobStatus.matched and job.llm_score == 88
                assert len(job.scores) == 1
                assert job.scores[0].description_chars == len(LONG)
    finally:
        if job_id:
            with TestSessionLocal() as cleanup:
                cleanup.query(Job).filter(Job.id == job_id).delete(synchronize_session=False)
                cleanup.commit()


@pytest.mark.parametrize("change", ["enrichment_and_manual_salary", "dismissal", "same_length_description"])
@pytest.mark.parametrize("mode", ["concurrent", "single"])
def test_an_intervening_writer_supersedes_a_match(monkeypatch, change, mode):
    """A separate committed edit wins over a model still reading its snapshot."""
    from sqlalchemy import text

    from tests.conftest import TestSessionLocal
    from app.services import enrichment, job_details

    # Real sessions are needed: the normal db fixture keeps its writes inside
    # an outer rollback transaction, invisible to the competing writer.
    ids = []
    screening_committed = threading.Event()
    evaluated_text = {}
    edited_text = {}
    monkeypatch.setattr(matcher, "_similarity_scorer", lambda *args: None)
    monkeypatch.setattr(job_details, "needs_extraction", lambda job: False)

    try:
        with TestSessionLocal() as setup:
            jobs = make_jobs(setup, 2 if mode == "concurrent" else 1,
                             salary_min=120000, salary_max=140000,
                             salary_currency="USD", salary_period="year",
                             salary_annual_min=120000, salary_annual_max=140000)
            ids = [job.id for job in jobs]

        def score(snapshot, *args, **kwargs):
            evaluated_text[snapshot.id] = snapshot.description
            assert snapshot.salary_label
            if snapshot.id != ids[-1]:
                return dict(REPLY)
            if mode == "concurrent":
                assert screening_committed.wait(5)
            with TestSessionLocal() as editing:
                # This must finish while the model call is still in flight;
                # a matching lock held across inference fails the test quickly.
                editing.execute(text("SET LOCAL lock_timeout = '2s'"))
                current = editing.get(Job, ids[-1])
                if change == "enrichment_and_manual_salary":
                    current.salary_min, current.salary_max = 180000, 200000
                    current.salary_annual_min, current.salary_annual_max = 180000, 200000
                    current.manual_fields = ["salary_min", "salary_max"]
                    enrichment.apply_extraction(editing, current, enrichment.Extraction(
                        description=LONG * 3, method="json_ld"))
                elif change == "dismissal":
                    current.status = JobStatus.filtered_out
                    current.filter_reason = "manual"
                    current.dismissed_at = datetime.now(timezone.utc)
                else:
                    current.description = "X" + current.description[1:]
                edited_text[ids[-1]] = current.description
                editing.commit()
            # A low score based on the obsolete stub must not strand the full
            # posting under a history entry claiming that it was already read.
            return {**REPLY, "score": 20}

        with TestSessionLocal() as matching:
            jobs = [matching.get(Job, job_id) for job_id in ids]
            real_commit = matching.commit

            def committed():
                real_commit()
                screening_committed.set()

            with patch.object(matching, "commit", side_effect=committed), \
                 patch.object(matcher, "llm_score_job", side_effect=score):
                if mode == "concurrent":
                    result = matcher._match_concurrently(matching, jobs, PROFILE,
                        "unused", "https://unused.invalid", "stub", {"paid_calls": 0}, 0, 2)
                    assert result["errors"] == 0
                    assert result["matched"] == 1
                    assert result["superseded"] == 1
                    assert result["filtered_out"] == 0
                else:
                    result = matcher.match_job(matching, jobs[0], PROFILE,
                        "unused", "https://unused.invalid", "stub", {"paid_calls": 0})
                    assert result == "superseded"
                    # The helper still leaves the caller in charge of commit.
                    assert not screening_committed.is_set()
                    matching.commit()

        with TestSessionLocal() as verifying:
            job = verifying.get(Job, ids[-1])
            assert job.description == edited_text[ids[-1]]
            assert job.llm_score is None
            assert job.scores == []
            assert job.applications == []
            if change == "dismissal":
                assert job.status == JobStatus.filtered_out
                assert job.filter_reason == "manual"
                return
            assert job.status == JobStatus.new
            if change == "enrichment_and_manual_salary":
                assert job.salary_min == 180000
                assert job.salary_annual_min == 180000
                assert job.manual_fields == ["salary_min", "salary_max"]
                assert len(job.description) > len(evaluated_text[ids[-1]])
            else:
                assert len(job.description) == len(evaluated_text[ids[-1]])

            # It remains eligible for a fresh pass, whose one recorded verdict
            # is tied to the text that pass actually evaluated.
            with patch.object(matcher, "llm_score_job", return_value=dict(REPLY)):
                retried = matcher._match_concurrently(verifying, [job], PROFILE,
                    "unused", "https://unused.invalid", "stub", {"paid_calls": 0}, 0, 2)
            assert retried["matched"] == 1
            verifying.refresh(job)
            assert len(job.scores) == 1
            assert job.scores[0].description_chars == len(edited_text[ids[-1]])
    finally:
        if ids:
            with TestSessionLocal() as cleanup:
                cleanup.query(Job).filter(Job.id.in_(ids)).delete(synchronize_session=False)
                cleanup.commit()


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
