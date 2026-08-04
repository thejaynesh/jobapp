"""
Tests for the response parser and the model-comparison tool.

The parser change is the load-bearing one: a reply it couldn't read used to
become score 0, which fell below the match threshold and filtered the job out
with the reason "AI scored this 0/100" — so a formatting hiccup silently
discarded a job and blamed the score for it.
"""

import hashlib
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.services.matcher import (
    ResponseParseError,
    _extract_json_object,
    _parse_llm_response,
)

_GOOD = ('{"score": 82, "reasoning": "Strong fit.", "matched_skills": ["Python"], '
         '"missing_skills": [], "seniority_fit": true}')


class TestResponseParsing:
    def test_plain_json(self):
        result = _parse_llm_response(_GOOD)
        assert result["score"] == 82
        assert result["matched_skills"] == ["Python"]

    def test_markdown_fenced_json(self):
        assert _parse_llm_response(f"```json\n{_GOOD}\n```")["score"] == 82

    def test_json_wrapped_in_prose(self):
        """Chattier models add a sentence either side."""
        reply = f"Sure, here is my assessment:\n{_GOOD}\nLet me know if you need more."
        assert _parse_llm_response(reply)["score"] == 82

    def test_json_after_a_reasoning_trace(self):
        """
        Reasoning models emit thinking first. Every strong model on NIM does
        this, so the parser has to cope or those models are unusable here.
        """
        reply = (
            "<think>The candidate has Python and FastAPI. The role wants Go. "
            "Seniority looks fine. I'll score around 80.</think>\n" + _GOOD
        )
        assert _parse_llm_response(reply)["score"] == 82

    def test_nested_braces_do_not_confuse_the_scan(self):
        reply = ('thinking... {"score": 55, "reasoning": "ok", '
                 '"matched_skills": [], "missing_skills": [], '
                 '"seniority_fit": false} trailing')
        assert _parse_llm_response(reply)["score"] == 55

    def test_a_float_score_is_accepted(self):
        assert _parse_llm_response('{"score": 77.6}')["score"] == 77

    def test_scores_are_clamped_to_the_valid_range(self):
        assert _parse_llm_response('{"score": 250}')["score"] == 100
        assert _parse_llm_response('{"score": -10}')["score"] == 0

    def test_missing_optional_fields_default_sensibly(self):
        result = _parse_llm_response('{"score": 60}')
        assert result["matched_skills"] == []
        # Absent seniority_fit must not silently apply the 15-point penalty.
        assert result["seniority_fit"] is True

    @pytest.mark.parametrize("reply", ["", "   ", "I cannot help with that.",
                                       "```json\nnot json\n```", "{oops"])
    def test_unreadable_replies_raise_rather_than_scoring_zero(self, reply):
        with pytest.raises(ResponseParseError):
            _parse_llm_response(reply)

    def test_a_reply_without_a_score_is_unreadable(self):
        with pytest.raises(ResponseParseError):
            _parse_llm_response('{"reasoning": "looks fine"}')

    def test_a_non_numeric_score_is_unreadable(self):
        with pytest.raises(ResponseParseError):
            _parse_llm_response('{"score": "very good"}')

    def test_extract_returns_the_first_object(self):
        assert _extract_json_object('{"a": 1} {"b": 2}') == {"a": 1}


class TestUnreadableRepliesDoNotFilterJobs:
    def test_the_job_is_left_for_retry_instead_of_scored_zero(self):
        """
        The whole point: an unreadable reply means we don't know, so the job
        stays `new` rather than being filtered out as a zero-scoring job.
        """
        from app.models.job import JobStatus
        from app.services.matcher import match_job

        job = MagicMock()
        job.title = "Backend Engineer"
        job.company = "Acme"
        job.location = "Remote"
        job.is_remote = True
        job.description = "Python FastAPI Docker Redis Kubernetes."
        job.status = JobStatus.new
        profile = {
            "target_roles": ["Backend Engineer"],
            "skills": {"languages": ["Python"], "tools": ["Docker", "Redis"]},
            "experience": [{"years": 3}],
        }

        with patch("app.services.matcher.chat_completion",
                   return_value="I think this is a decent match, honestly."), \
             patch("app.services.matcher.matching_fallbacks", return_value=[]):
            result = match_job(MagicMock(), job, profile, "k", "u", "m")

        assert result == "rate_limited"       # the "leave it for next time" path
        assert job.status == JobStatus.new
        assert job.filter_reason is not MagicMock  # never set to low_score


class TestModelCompare:
    def _job(self, db, title="Backend Engineer", description="Python and Go."):
        from app.models.job import Job, JobStatus
        url = f"https://ex.com/{title}"
        job = Job(
            source="linkedin", source_urls=[url], title=title, company="Acme",
            location="NYC", is_remote=False, url=url, description=description,
            experience_level="mid", status=JobStatus.new,
            fetched_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            dedupe_hash=hashlib.sha256(url.encode()).hexdigest()[:32],
        )
        db.add(job)
        db.commit()
        return job

    def test_only_jobs_with_descriptions_are_sampled(self, db):
        from app.services.model_compare import sample_jobs
        self._job(db, title="With Text", description="Python and Go.")
        self._job(db, title="No Text", description="")
        assert [j.title for j in sample_jobs(db, 10)] == ["With Text"]

    def test_each_model_scores_the_same_jobs(self, db):
        from app.services.model_compare import compare_models
        self._job(db, title="A")
        self._job(db, title="B")

        with patch("app.services.matcher.chat_completion", return_value=_GOOD):
            jobs, results = compare_models(db, ["model-x", "model-y"], limit=5)

        assert len(jobs) == 2
        assert [r.model for r in results] == ["model-x", "model-y"]
        assert all(r.scored == 2 for r in results)
        assert results[0].average == 82

    def test_unreadable_replies_are_counted_separately_from_errors(self, db):
        from app.services.model_compare import compare_models
        self._job(db, title="A")

        with patch("app.services.matcher.chat_completion",
                   return_value="thinking out loud, no json here"):
            _, results = compare_models(db, ["chatty-model"], limit=5)

        assert results[0].unreadable == 1
        assert results[0].errors == 0
        assert results[0].scored == 0

    def test_a_failing_call_is_counted_as_an_error(self, db):
        from app.services.model_compare import compare_models
        self._job(db, title="A")

        with patch("app.services.matcher.chat_completion",
                   side_effect=RuntimeError("503")):
            _, results = compare_models(db, ["broken-model"], limit=5)

        assert results[0].errors == 1
        assert results[0].unreadable == 0

    def test_the_report_flags_verdict_flips_across_the_threshold(self, db):
        from app.services.model_compare import ModelResult, format_report
        job = self._job(db, title="Borderline")
        a = ModelResult(model="old", scores={str(job.id): 65})
        b = ModelResult(model="new", scores={str(job.id): 80})

        report = format_report([job], [a, b], threshold=70)
        assert "Verdict flips" in report
        assert "65 → 80" in report

    def test_the_report_says_so_when_models_agree(self, db):
        from app.services.model_compare import ModelResult, format_report
        job = self._job(db, title="Clear")
        a = ModelResult(model="old", scores={str(job.id): 80})
        b = ModelResult(model="new", scores={str(job.id): 85})
        assert "agree on every job" in format_report([job], [a, b], threshold=70)

    def test_the_report_warns_about_unreadable_replies(self, db):
        from app.services.model_compare import ModelResult, format_report
        job = self._job(db, title="X")
        result = ModelResult(model="chatty", unreadable=3)
        assert "poor fit for this prompt" in format_report([job], [result], 70)

    def test_an_empty_database_reports_cleanly(self, db):
        from app.services.model_compare import compare_models, format_report
        jobs, results = compare_models(db, ["m"], limit=5)
        assert "fetch some first" in format_report(jobs, results, 70)
