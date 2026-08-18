"""
The second opinion, on the jobs where the answer is actually in doubt.

A fast model scores everything, and most of its answers are not close calls: a
20 is a 20 and a 95 is a 95 whoever reads them. The band in the middle is where
accept and reject flip, and where a cheap model's guess decides whether a job
is ever seen — so those get scored again by the strongest configured provider,
and both numbers are kept.
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.config import settings
from app.llm.providers import Provider
from app.models.job import Job, JobStatus
from app.models.profile import Profile


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
}

_STRONG = Provider(name="anthropic", api_key="k", model="claude-opus-4-8")


def _first_pass(score, **extra):
    result = {"score": score, "reasoning": "first pass", "matched_skills": ["Python"],
              "missing_skills": [], "seniority_fit": True, "scored_by": "nim/glm"}
    result.update(extra)
    return result


def _deep_reply(score, reasoning="second pass"):
    import json
    return json.dumps({
        "score": score, "reasoning": reasoning,
        "matched_skills": ["Python", "Go"], "missing_skills": ["Rust"],
        "seniority_fit": True,
    })


class TestWhenTheSecondPassRuns:
    def _run(self, db, first_score, deep_score=90, provider=_STRONG):
        db.add(Profile(data=PROFILE))
        job = _job()
        db.add(job)
        db.commit()

        from app.services.matcher import match_job

        with patch("app.services.matcher.llm_score_job",
                   return_value=_first_pass(first_score)), \
             patch("app.llm.providers.deep_matching_provider", return_value=provider), \
             patch("app.services.matcher.call_provider",
                   return_value=_deep_reply(deep_score)) as call:
            outcome = match_job(db, job, PROFILE, "k", "u", "m")
        return job, call, outcome

    def test_a_close_call_is_scored_again(self, db):
        job, call, _ = self._run(db, first_score=62)
        call.assert_called_once()
        assert job.llm_score == 62
        assert job.llm_score_deep == 90
        assert job.deep_matched_by == "anthropic/claude-opus-4-8"

    def test_a_confident_reject_is_not(self, db):
        # A 20 is a 20 whoever reads it; the call would buy nothing.
        job, call, _ = self._run(db, first_score=20)
        call.assert_not_called()
        assert job.llm_score_deep is None

    def test_a_confident_accept_is_not(self, db):
        job, call, _ = self._run(db, first_score=95)
        call.assert_not_called()
        assert job.llm_score_deep is None

    def test_the_band_edges_are_included(self, db):
        for score in (settings.DEEP_MATCH_BAND_LOW, settings.DEEP_MATCH_BAND_HIGH):
            _, call, _ = self._run(db, first_score=score)
            call.assert_called_once()

    def test_nothing_stronger_configured_means_no_second_pass(self, db):
        """
        Re-asking the same model the same question spends a call to hear the
        same answer.
        """
        job, call, _ = self._run(db, first_score=62, provider=None)
        call.assert_not_called()
        assert job.llm_score_deep is None

    def test_it_can_be_switched_off(self, db):
        with patch.object(settings, "DEEP_MATCH_ENABLED", False):
            job, call, _ = self._run(db, first_score=62)
        call.assert_not_called()
        assert job.llm_score_deep is None


class TestWhatTheSecondPassDecides:
    def _run(self, db, first_score, deep_score, min_score=70):
        profile = {**PROFILE, "min_match_score": min_score}
        db.add(Profile(data=profile))
        job = _job()
        db.add(job)
        db.commit()

        from app.services.matcher import match_job

        with patch("app.services.matcher.llm_score_job",
                   return_value=_first_pass(first_score)), \
             patch("app.llm.providers.deep_matching_provider", return_value=_STRONG), \
             patch("app.services.matcher.call_provider",
                   return_value=_deep_reply(deep_score)):
            outcome = match_job(db, job, profile, "k", "u", "m")
        return job, outcome

    def test_the_deep_score_decides_the_outcome(self, db):
        # First pass said no, the stronger model said yes. The point of asking.
        job, outcome = self._run(db, first_score=62, deep_score=88)
        assert outcome == "matched"
        assert job.status == JobStatus.matched

    def test_it_can_also_reject_what_the_first_pass_accepted(self, db):
        job, outcome = self._run(db, first_score=74, deep_score=40)
        assert outcome == "filtered_out"
        assert job.filter_reason == "low_score"
        # The reason quotes the number the decision actually used.
        assert "40" in job.filter_detail

    def test_the_deep_reasoning_replaces_the_first(self, db):
        # Keeping the first pass's would describe a verdict nobody acted on.
        job, _ = self._run(db, first_score=62, deep_score=88)
        assert job.llm_reasoning == "second pass"
        assert job.missing_skills == ["Rust"]

    def test_both_numbers_survive(self, db):
        job, _ = self._run(db, first_score=62, deep_score=88)
        assert job.llm_score == 62
        assert job.llm_score_deep == 88
        assert job.matched_by == "nim/glm"
        assert job.deep_matched_by == "anthropic/claude-opus-4-8"

    def test_the_seniority_penalty_applies_to_the_deep_score_too(self, db):
        import json

        db.add(Profile(data=PROFILE))
        job = _job()
        db.add(job)
        db.commit()

        from app.services.matcher import match_job

        reply = json.dumps({"score": 80, "reasoning": "r", "matched_skills": [],
                            "missing_skills": [], "seniority_fit": False})
        with patch("app.services.matcher.llm_score_job",
                   return_value=_first_pass(62)), \
             patch("app.llm.providers.deep_matching_provider", return_value=_STRONG), \
             patch("app.services.matcher.call_provider", return_value=reply):
            match_job(db, job, PROFILE, "k", "u", "m")

        assert job.llm_score_deep == 65  # 80 - 15


class TestFailureAndBudget:
    def _job_and_profile(self, db):
        db.add(Profile(data=PROFILE))
        job = _job()
        db.add(job)
        db.commit()
        return job

    def test_a_failed_second_pass_keeps_the_first_score(self, db):
        """
        The first score is a real answer. Losing it because a second opinion
        was unavailable would be strictly worse than not asking.
        """
        from app.services.matcher import match_job

        job = self._job_and_profile(db)
        with patch("app.services.matcher.llm_score_job",
                   return_value=_first_pass(62)), \
             patch("app.llm.providers.deep_matching_provider", return_value=_STRONG), \
             patch("app.services.matcher.call_provider",
                   side_effect=RuntimeError("provider down")):
            outcome = match_job(db, job, PROFILE, "k", "u", "m")

        assert outcome == "filtered_out"   # 62 is below the default minimum
        assert job.llm_score == 62
        assert job.llm_score_deep is None

    def test_an_unreadable_deep_reply_keeps_the_first_score(self, db):
        from app.services.matcher import match_job

        job = self._job_and_profile(db)
        with patch("app.services.matcher.llm_score_job",
                   return_value=_first_pass(62)), \
             patch("app.llm.providers.deep_matching_provider", return_value=_STRONG), \
             patch("app.services.matcher.call_provider", return_value="not json"):
            match_job(db, job, PROFILE, "k", "u", "m")

        assert job.llm_score_deep is None

    def test_the_budget_stops_the_second_pass(self, db):
        from app.services.matcher import match_job

        job = self._job_and_profile(db)
        budget = {"paid_calls": 0, "deep_calls": settings.DEEP_MATCH_MAX_PER_CYCLE}
        with patch("app.services.matcher.llm_score_job",
                   return_value=_first_pass(62)), \
             patch("app.llm.providers.deep_matching_provider", return_value=_STRONG), \
             patch("app.services.matcher.call_provider") as call:
            match_job(db, job, PROFILE, "k", "u", "m", budget=budget)

        call.assert_not_called()
        assert job.llm_score == 62

    def test_a_failed_call_still_counts_against_the_budget(self, db):
        """
        A failed paid call can still be a billed one, and a provider erroring
        on every job would otherwise retry through the whole batch.
        """
        from app.services.matcher import match_job

        job = self._job_and_profile(db)
        budget = {"paid_calls": 0, "deep_calls": 0}
        with patch("app.services.matcher.llm_score_job",
                   return_value=_first_pass(62)), \
             patch("app.llm.providers.deep_matching_provider", return_value=_STRONG), \
             patch("app.services.matcher.call_provider",
                   side_effect=RuntimeError("boom")):
            match_job(db, job, PROFILE, "k", "u", "m", budget=budget)

        assert budget["deep_calls"] == 1


class TestProviderChoice:
    def test_quality_order_not_free_first(self):
        """
        Both other chains start with FreeInference because they run on
        everything. This one is the opposite case: a second opinion from a
        model no better than the first is not a second opinion.
        """
        from app.llm.providers import DEEP_MATCHING_PREFERENCE, GENERATION_PREFERENCE

        assert DEEP_MATCHING_PREFERENCE[0] == "anthropic"
        assert GENERATION_PREFERENCE[0] == "freeinference"

    def test_none_when_only_the_primary_is_configured(self):
        from app.llm.providers import deep_matching_provider

        with patch("app.llm.providers.configured_providers", return_value={}):
            assert deep_matching_provider() is None

    def test_it_uses_the_generation_model_not_the_cheap_matching_one(self):
        # A second opinion from the cut-down model is the same compromise twice.
        from app.llm.providers import deep_matching_provider

        with patch("app.llm.providers.configured_providers",
                   return_value={"anthropic": _STRONG}):
            assert deep_matching_provider().model == "claude-opus-4-8"


class TestTheUiShowsTheDecidingScore:
    def _scored(self, db, **kwargs):
        job = _job(status=JobStatus.matched, **kwargs)
        db.add(job)
        db.commit()
        return job

    def test_the_card_shows_the_second_score_and_names_the_first(self, client, db):
        self._scored(db, title="Reconsidered Engineer", llm_score=62,
                     llm_score_deep=88, matched_by="nim/glm",
                     deep_matched_by="anthropic/claude-opus-4-8")
        body = client.get("/jobs").text
        assert "88" in body
        assert "second look" in body
        assert "was 62" in body

    def test_a_single_pass_job_reads_exactly_as_before(self, client, db):
        self._scored(db, title="Plain Engineer", llm_score=91, matched_by="nim/glm")
        body = client.get("/jobs").text
        assert "91" in body
        assert "second look" not in body

    def test_the_score_filter_uses_the_deciding_number(self, client, db):
        # Filtering on the first pass while showing the second would hide a job
        # the page says scores 88.
        self._scored(db, title="Reconsidered Engineer", llm_score=62,
                     llm_score_deep=88)
        assert "Reconsidered Engineer" in client.get("/jobs?min_score=80").text

    def test_the_deciding_number_is_what_sorts(self, client, db):
        self._scored(db, title="Deeply Reconsidered", llm_score=10, llm_score_deep=99)
        self._scored(db, title="Merely Good", llm_score=80)
        body = client.get("/jobs?sort=score_desc").text
        assert body.index("Deeply Reconsidered") < body.index("Merely Good")

    def test_the_model_property_agrees_with_the_sql(self, db):
        job = self._scored(db, llm_score=62, llm_score_deep=88)
        assert job.effective_score == 88
        plain = self._scored(db, llm_score=62)
        assert plain.effective_score == 62
