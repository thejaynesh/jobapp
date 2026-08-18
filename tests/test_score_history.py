"""
The verdict a job used to have.

Matching writes its answer into the job row, so every re-evaluation destroys
the one before it — and jobs are now re-evaluated routinely, because enrichment
sends one back the moment its description grows. Without a history, the job most
worth understanding (the one the pipeline changed its mind about) is exactly the
job whose earlier verdict no longer exists.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.models.job import Job, JobStatus
from app.models.job_score import JobScore
from app.models.profile import Profile
from app.services import score_history


@pytest.fixture(autouse=True)
def _no_detail_extraction(monkeypatch):
    """Detail extraction is a live LLM call and a different feature's test."""
    from app.services import job_details

    monkeypatch.setattr(job_details, "needs_extraction", lambda job: False)


def _job(**kwargs) -> Job:
    defaults = dict(
        source="greenhouse",
        source_urls=[f"https://x/{uuid.uuid4()}"],
        title="Backend Engineer",
        company="Acme",
        location="Remote",
        url=f"https://x/{uuid.uuid4()}",
        description="We need a backend engineer with Python and Go. " * 20,
        status=JobStatus.new,
        fetched_at=datetime.now(timezone.utc),
        dedupe_hash=uuid.uuid4().hex,
    )
    defaults.update(kwargs)
    return Job(**defaults)


PROFILE = {
    "target_roles": ["Backend Engineer"],
    "skills": {"lang": ["Python", "Go"]},
    "experience": [{"role": "Engineer", "company": "Acme",
                    "start_date": "Jan 2019", "end_date": "Aug 2026"}],
    "min_match_score": 60,
}


def _reply(score, reasoning="because", seniority_fit=True):
    return {"score": score, "reasoning": reasoning, "matched_skills": ["Python"],
            "missing_skills": ["Rust"], "seniority_fit": seniority_fit,
            "scored_by": "nim/glm"}


def _score(db, job, score, profile=PROFILE, reasoning="because"):
    from app.services.matcher import match_job

    with patch("app.services.matcher.llm_score_job",
               return_value=_reply(score, reasoning)), \
         patch("app.llm.providers.deep_matching_provider", return_value=None):
        outcome = match_job(db, job, profile, "k", "u", "m")
    db.commit()
    return outcome


def _rows(db, job) -> list[JobScore]:
    return score_history.history(db, job.id)


class TestEveryEvaluationIsKept:
    def test_the_first_score_is_recorded(self, db):
        db.add(Profile(data=PROFILE))
        job = _job()
        db.add(job)
        db.commit()

        _score(db, job, 82)

        rows = _rows(db, job)
        assert len(rows) == 1
        assert rows[0].score == 82
        assert rows[0].status == "matched"
        assert rows[0].reasoning == "because"
        assert rows[0].trigger == "initial"

    def test_a_rejection_that_is_later_overturned_keeps_both(self, db):
        # The case the whole table exists for: rejected on a thin stub, then
        # enrichment brings the real posting in and it is accepted. Showing
        # only the 82 loses the evidence that the pipeline changed its mind.
        db.add(Profile(data=PROFILE))
        job = _job(description="Backend engineer. Python and Go.")
        db.add(job)
        db.commit()

        _score(db, job, 45, reasoning="not much to go on")
        assert job.status == JobStatus.filtered_out
        assert job.filter_reason == "low_score"

        job.description = "We need a backend engineer with Python and Go. " * 40
        job.description_updated_at = datetime.now(timezone.utc)
        job.status = JobStatus.new
        job.filter_reason = None
        db.commit()
        _score(db, job, 82, reasoning="the real posting is a good fit")

        rows = _rows(db, job)
        assert [r.score for r in rows] == [82, 45]
        assert rows[1].status == "filtered_out"
        assert rows[1].filter_reason == "low_score"
        assert rows[1].reasoning == "not much to go on"
        # And what each verdict was reached ON — the part that says the 45 was
        # a judgement about a stub rather than about the job.
        assert rows[1].description_chars < rows[0].description_chars

    def test_the_threshold_of_the_day_is_recorded(self, db):
        # Otherwise "scored 58, filtered out" reads as a misfire the day the
        # user moves their minimum to 50.
        db.add(Profile(data=PROFILE))
        job = _job()
        db.add(job)
        db.commit()

        _score(db, job, 58)
        assert _rows(db, job)[0].min_score == 60

    def test_both_passes_are_recorded_when_there_were_two(self, db):
        from app.llm.providers import Provider
        from app.services.matcher import match_job

        db.add(Profile(data=PROFILE))
        job = _job()
        db.add(job)
        db.commit()

        deep = json.dumps({"score": 88, "reasoning": "second pass",
                           "matched_skills": ["Go"], "missing_skills": [],
                           "seniority_fit": True})
        with patch("app.services.matcher.llm_score_job", return_value=_reply(62)), \
             patch("app.llm.providers.deep_matching_provider",
                   return_value=Provider(name="anthropic", api_key="k", model="opus")), \
             patch("app.services.matcher.call_provider", return_value=deep):
            match_job(db, job, PROFILE, "k", "u", "m")
        db.commit()

        row = _rows(db, job)[0]
        assert row.llm_score == 62
        assert row.llm_score_deep == 88
        assert row.score == 88          # the number the decision used
        assert row.deep_matched_by == "anthropic/opus"

    def test_a_keyword_rejection_is_recorded_without_a_score(self, db):
        db.add(Profile(data=PROFILE))
        job = _job(title="Head Chef")
        db.add(job)
        db.commit()

        _score(db, job, 90)   # never reached — the title gate fires first

        row = _rows(db, job)[0]
        assert row.score is None
        assert row.filter_reason == "title_mismatch"
        # No model ran, so the reasoning column must not carry one over from a
        # previous evaluation and attribute it to this verdict.
        assert row.reasoning is None

    def test_a_rate_limited_pass_records_nothing(self, db):
        # Nothing was decided: the job stays `new` and is retried next cycle.
        from app.services.matcher import LLMUnavailableError, match_job

        db.add(Profile(data=PROFILE))
        job = _job()
        db.add(job)
        db.commit()

        with patch("app.services.matcher.llm_score_job",
                   side_effect=LLMUnavailableError("all providers down")):
            outcome = match_job(db, job, PROFILE, "k", "u", "m")
        db.commit()

        assert outcome == "rate_limited"
        assert _rows(db, job) == []


class TestWhyItWasScoredAgain:
    def test_a_fuller_description_is_named_as_the_reason(self, db):
        db.add(Profile(data=PROFILE))
        job = _job()
        db.add(job)
        db.commit()

        _score(db, job, 45)
        job.description_updated_at = datetime.now(timezone.utc) + timedelta(seconds=5)
        job.status = JobStatus.new
        db.commit()
        _score(db, job, 82)

        assert _rows(db, job)[0].trigger == "description_grew"

    def test_a_re_score_on_the_same_text_is_not(self, db):
        db.add(Profile(data=PROFILE))
        job = _job()
        db.add(job)
        db.commit()

        _score(db, job, 45)
        job.status = JobStatus.new
        db.commit()
        _score(db, job, 47)

        assert _rows(db, job)[0].trigger == "rescored"

    def test_a_description_stamp_older_than_the_last_verdict_is_not(self, db):
        # The description grew before the last evaluation, so that evaluation
        # already saw it — this one is a plain re-score.
        db.add(Profile(data=PROFILE))
        job = _job(description_updated_at=datetime.now(timezone.utc)
                   - timedelta(days=3))
        db.add(job)
        db.commit()

        _score(db, job, 45)
        job.status = JobStatus.new
        db.commit()
        _score(db, job, 45)

        assert _rows(db, job)[0].trigger == "rescored"


class TestItStaysBounded:
    def test_old_evaluations_are_dropped_per_job(self, db, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "SCORE_HISTORY_KEEP_PER_JOB", 3)
        db.add(Profile(data=PROFILE))
        job = _job()
        db.add(job)
        db.commit()

        for score in (10, 20, 30, 40, 50):
            job.status = JobStatus.new
            _score(db, job, score)

        rows = _rows(db, job)
        assert [r.score for r in rows] == [50, 40, 30]

    def test_one_job_pruning_leaves_another_alone(self, db, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "SCORE_HISTORY_KEEP_PER_JOB", 1)
        db.add(Profile(data=PROFILE))
        first, second = _job(), _job()
        db.add_all([first, second])
        db.commit()

        _score(db, first, 70)
        _score(db, second, 80)
        second.status = JobStatus.new
        _score(db, second, 85)

        assert [r.score for r in _rows(db, first)] == [70]
        assert [r.score for r in _rows(db, second)] == [85]


class TestItNeverBreaksMatching:
    def test_a_job_that_was_never_stored_is_skipped(self, db):
        # The match-quality fixture scores job-shaped objects with no id.
        class _Fixture:
            id = None
            description = "text"

        assert score_history.record(db, _Fixture()) is None

    def test_a_failure_to_build_the_row_costs_only_the_row(self, db):
        db.add(Profile(data=PROFILE))
        job = _job()
        db.add(job)
        db.commit()

        with patch("app.services.score_history._trigger",
                   side_effect=RuntimeError("boom")):
            assert score_history.record(db, job, profile_data=PROFILE) is None

        # The session is still usable, which is the point: a broken history row
        # must not take the score it was recording down with it.
        db.commit()
        assert _rows(db, job) == []


class TestWhatTheCardShows:
    def test_a_single_evaluation_is_not_worth_a_widget(self, db):
        db.add(Profile(data=PROFILE))
        job = _job()
        db.add(job)
        db.commit()
        _score(db, job, 82)

        db.refresh(job)
        assert job.scores != []
        assert job.score_history == []

    def test_more_than_one_is(self, db):
        db.add(Profile(data=PROFILE))
        job = _job()
        db.add(job)
        db.commit()
        _score(db, job, 45)
        job.status = JobStatus.new
        _score(db, job, 82)

        db.refresh(job)
        assert len(job.score_history) == 2
        # Newest first, so the card leads with the verdict in force.
        assert job.score_history[0].score == 82

    def test_the_history_appears_on_the_jobs_page(self, client, db):
        db.add(Profile(data=PROFILE))
        job = _job(title="Backend Engineer", company="Historic Co")
        db.add(job)
        db.commit()
        _score(db, job, 45)
        job.status = JobStatus.new
        _score(db, job, 82)

        body = client.get("/jobs").text
        assert "Scored 2 times" in body
        assert "was 45, now 82" in body


class TestAKeywordRejectionClearsTheOldScore:
    def test_the_deep_score_goes_too(self, db):
        # `effective_score` reads llm_score_deep first, so leaving it behind
        # showed 88 beside "filtered out" — and would have recorded a score
        # this evaluation never gave.
        db.add(Profile(data=PROFILE))
        job = _job(llm_score=62, llm_score_deep=88, deep_matched_by="anthropic/opus",
                   title="Head Chef")
        db.add(job)
        db.commit()

        _score(db, job, 90)

        assert job.llm_score is None
        assert job.llm_score_deep is None
        assert job.deep_matched_by is None
        assert job.effective_score is None
