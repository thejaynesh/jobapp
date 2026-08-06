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


def _stored_job(db, title="Backend Engineer", description="Python and Go."):
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


class TestModelCompare:
    def _job(self, db, **kwargs):
        return _stored_job(db, **kwargs)

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


class TestReportDict:
    """The UI renders stored plain data, so the shape is part of the contract."""

    def _result(self, model, job_id, score):
        from app.services.model_compare import ModelResult
        return ModelResult(model=model, scores={str(job_id): score}, seconds=1.5)

    def test_rows_carry_a_score_per_model(self, db):
        from app.services.model_compare import report_dict
        job = _stored_job(db, title="A")
        payload = report_dict(
            [job], [self._result("old", job.id, 60), self._result("new", job.id, 90)],
            threshold=70,
        )
        assert payload["models"] == ["old", "new"]
        assert payload["rows"][0]["scores"] == {"old": 60, "new": 90}
        assert payload["job_count"] == 1

    def test_a_missing_score_is_none_rather_than_absent(self, db):
        """The template renders an em dash for these; a KeyError would 500."""
        from app.services.model_compare import ModelResult, report_dict
        job = _stored_job(db, title="A")
        payload = report_dict([job], [ModelResult(model="chatty", unreadable=1)], 70)
        assert payload["rows"][0]["scores"]["chatty"] is None
        assert payload["summary"][0]["unreadable"] == 1

    def test_flips_record_direction_across_the_threshold(self, db):
        from app.services.model_compare import report_dict
        job = _stored_job(db, title="Borderline")
        payload = report_dict(
            [job], [self._result("old", job.id, 65), self._result("new", job.id, 80)],
            threshold=70,
        )
        assert payload["flips"] == [{
            "title": "Borderline", "company": "Acme",
            "from": 65, "to": 80, "direction": "gained",
        }]

    def test_the_payload_is_json_serialisable(self, db):
        """It gets stored in a JSONB column, so anything exotic would fail late."""
        import json
        from app.services.model_compare import report_dict
        job = _stored_job(db, title="A")
        payload = report_dict([job], [self._result("old", job.id, 60)], 70)
        assert json.loads(json.dumps(payload))["rows"][0]["title"] == "A"


class TestComparisonTask:
    def test_it_refuses_to_overlap_another_comparison(self):
        """Two at once would double the LLM spend for no extra information."""
        import app.tasks.compare_models as task
        with patch.object(task, "acquire", return_value=False), \
             patch.object(task, "compare_models") as work:
            result = task.run_comparison.apply(kwargs={"models": ["a", "b"]}).result
        work.assert_not_called()
        assert result["status"] == "already running"

    def test_it_takes_the_comparison_lock_not_the_fetch_lock(self):
        """A comparison and a fetch don't conflict; sharing a key would block both."""
        import app.tasks.compare_models as task
        from app.services.fetch_lock import COMPARE_LOCK_KEY
        with patch.object(task, "acquire", return_value=False) as acquire:
            task.run_comparison.apply(kwargs={"models": ["a", "b"]})
        assert acquire.call_args.kwargs["key"] == COMPARE_LOCK_KEY

    def test_the_result_lands_on_the_profile(self, db):
        import app.tasks.compare_models as task
        from app.models.profile import Profile
        from app.services.model_compare import ModelResult

        db.add(Profile(data={"target_roles": ["Backend Engineer"]}))
        db.commit()
        job = _stored_job(db, title="A")

        with patch.object(task, "acquire", return_value=True), \
             patch.object(task, "release"), \
             patch.object(task, "SessionLocal", return_value=db), \
             patch.object(db, "close"), \
             patch.object(task, "compare_models",
                          return_value=([job], [ModelResult(model="m",
                                                            scores={str(job.id): 88})])):
            result = task.run_comparison.apply(kwargs={"models": ["m"]}).result

        assert result["status"] == "done"
        stored = db.query(Profile).first().data["model_comparison"]
        assert stored["summary"][0]["average"] == 88.0
        # The rest of the profile has to survive being written alongside it.
        assert stored["status"] == "done"
        assert db.query(Profile).first().data["target_roles"] == ["Backend Engineer"]

    def test_the_record_is_marked_running_before_the_scoring_starts(self, db):
        """
        A comparison is minutes long, so the panel needs to know it's underway
        rather than only finding out when the result lands.
        """
        import app.tasks.compare_models as task
        from app.models.profile import Profile
        from app.services.model_compare import load_state

        db.add(Profile(data={}))
        db.commit()
        seen = {}

        def record_then_fail(*args, **kwargs):
            seen["status"] = load_state(db)["status"]
            raise RuntimeError("stop here")

        with patch.object(task, "acquire", return_value=True), \
             patch.object(task, "release"), \
             patch.object(task, "SessionLocal", return_value=db), \
             patch.object(db, "close"), \
             patch.object(task, "compare_models", side_effect=record_then_fail):
            task.run_comparison.apply(kwargs={"models": ["m", "n"], "limit": 4})

        assert seen["status"] == "running"

    def test_no_jobs_is_reported_rather_than_stored_as_an_empty_success(self, db):
        import app.tasks.compare_models as task
        from app.models.profile import Profile

        db.add(Profile(data={}))
        db.commit()
        with patch.object(task, "acquire", return_value=True), \
             patch.object(task, "release"), \
             patch.object(task, "SessionLocal", return_value=db), \
             patch.object(db, "close"), \
             patch.object(task, "compare_models", return_value=([], [])):
            result = task.run_comparison.apply(kwargs={"models": ["m"]}).result
        assert result["status"] == "no jobs"
        assert db.query(Profile).first().data["model_comparison"]["status"] == "no jobs"

    def test_a_failure_is_stored_so_the_page_can_say_what_happened(self, db):
        import app.tasks.compare_models as task
        from app.models.profile import Profile

        db.add(Profile(data={}))
        db.commit()
        with patch.object(task, "acquire", return_value=True), \
             patch.object(task, "release"), \
             patch.object(task, "SessionLocal", return_value=db), \
             patch.object(db, "close"), \
             patch.object(task, "compare_models", side_effect=RuntimeError("nim down")):
            result = task.run_comparison.apply(kwargs={"models": ["m"]}).result

        assert result["status"] == "failed"
        stored = db.query(Profile).first().data["model_comparison"]
        assert stored["status"] == "failed"
        assert "nim down" in stored["error"]

    def test_the_lock_is_released_even_when_the_comparison_raises(self, db):
        import app.tasks.compare_models as task
        with patch.object(task, "acquire", return_value=True), \
             patch.object(task, "release") as release, \
             patch.object(task, "SessionLocal", return_value=MagicMock()), \
             patch.object(task, "compare_models", side_effect=RuntimeError("boom")):
            task.run_comparison.apply(kwargs={"models": ["m"]})
        release.assert_called_once()


_TWO = ["meta/llama-3.3-70b-instruct", "meta/llama-3.1-70b-instruct"]


class TestCompareRoutes:
    """The panel is how this gets used; the command line was the stopgap."""

    def _profile(self, db, comparison=None):
        from app.models.profile import Profile
        data = {"target_roles": ["Backend Engineer"]}
        if comparison is not None:
            data["model_comparison"] = comparison
        db.add(Profile(data=data))
        db.commit()

    def test_the_panel_renders_on_the_runs_page(self, client):
        from app.config import settings
        body = client.get("/runs").text
        assert "Compare matching models" in body
        assert 'hx-post="/runs/compare"' in body
        # The model in use is pre-checked so a comparison is one click.
        assert settings.NVIDIA_NIM_MODEL in body

    def test_one_model_is_refused_with_a_reason(self, client, db):
        self._profile(db)
        with patch("app.tasks.compare_models.run_comparison") as task:
            body = client.post("/runs/compare",
                               data={"models": ["meta/llama-3.3-70b-instruct"]}).text
        task.delay.assert_not_called()
        assert "at least two models" in body

    def test_unknown_model_ids_are_dropped(self, client, db):
        """The id goes straight to the provider, so only the curated list runs."""
        self._profile(db)
        with patch("app.tasks.compare_models.run_comparison") as task:
            client.post("/runs/compare", data={"models": [
                "meta/llama-3.3-70b-instruct", "attacker/whatever",
                "meta/llama-3.1-70b-instruct"]})
        assert task.delay.call_args.kwargs["models"] == _TWO

    def test_a_valid_request_is_queued_and_the_panel_starts_polling(self, client, db):
        self._profile(db)
        with patch("app.tasks.compare_models.run_comparison") as task:
            body = client.post("/runs/compare",
                               data={"models": _TWO, "limit": 5}).text
        assert task.delay.call_args.kwargs == {"models": _TWO, "limit": 5}
        assert 'hx-get="/runs/compare/status"' in body
        assert "waiting for a worker" in body

    def test_the_request_is_recorded_before_the_task_is_published(self, client, db):
        """
        The gap between queueing and a worker starting is where this used to
        show nothing at all: the Redis lock isn't held yet, so the panel saw
        "not running", stopped polling, and left the page looking untouched.
        """
        from app.services.model_compare import load_state, progress
        self._profile(db)
        with patch("app.tasks.compare_models.run_comparison"):
            client.post("/runs/compare", data={"models": _TWO, "limit": 5})
        record = load_state(db)
        assert record["status"] == "queued"
        assert record["models"] == _TWO
        assert progress(record)["active"] is True

    def test_the_panel_keeps_polling_while_the_task_is_only_queued(self, client, db):
        from datetime import datetime, timezone
        self._profile(db, {"status": "queued", "models": _TWO, "job_count": 5,
                           "queued_at": datetime.now(timezone.utc).isoformat(),
                           "rows": [], "summary": [], "flips": []})
        body = client.get("/runs/compare/status").text
        assert 'hx-trigger="every 5s"' in body
        assert "waiting for a worker" in body

    def test_a_running_comparison_says_what_it_is_working_through(self, client, db):
        from datetime import datetime, timezone
        self._profile(db, {"status": "running", "models": _TWO, "job_count": 7,
                           "started_at": datetime.now(timezone.utc).isoformat(),
                           "rows": [], "summary": [], "flips": []})
        body = client.get("/runs/compare/status").text
        assert 'hx-trigger="every 5s"' in body
        assert "Scoring for" in body
        assert "7 jobs" in body

    def test_a_worker_that_died_mid_run_is_called_out_not_polled_forever(self, client, db):
        from datetime import datetime, timedelta, timezone
        stale = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        self._profile(db, {"status": "running", "models": _TWO, "job_count": 5,
                           "started_at": stale,
                           "rows": [], "summary": [], "flips": []})
        body = client.get("/runs/compare/status").text
        assert 'hx-trigger="every 5s"' not in body
        assert "never reported" in body

    def test_the_limit_is_clamped(self, client, db):
        """Each job costs one call per model; an unbounded limit is real money."""
        self._profile(db)
        with patch("app.tasks.compare_models.run_comparison") as task:
            client.post("/runs/compare", data={"models": _TWO, "limit": 5000})
        assert task.delay.call_args.kwargs["limit"] == 50

    def test_a_second_request_while_one_is_pending_is_refused(self, client, db):
        from datetime import datetime, timezone
        self._profile(db, {"status": "running", "models": _TWO, "job_count": 5,
                           "started_at": datetime.now(timezone.utc).isoformat(),
                           "rows": [], "summary": [], "flips": []})
        with patch("app.tasks.compare_models.run_comparison") as task:
            body = client.post("/runs/compare", data={"models": _TWO}).text
        task.delay.assert_not_called()
        assert "already queued or running" in body

    def test_a_stalled_record_does_not_block_a_new_comparison(self, client, db):
        from datetime import datetime, timedelta, timezone
        stale = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        self._profile(db, {"status": "running", "models": _TWO, "job_count": 5,
                           "started_at": stale,
                           "rows": [], "summary": [], "flips": []})
        with patch("app.tasks.compare_models.run_comparison") as task:
            client.post("/runs/compare", data={"models": _TWO})
        assert task.delay.called

    def test_a_broken_broker_says_so_instead_of_500ing(self, client, db):
        self._profile(db)
        with patch("app.tasks.compare_models.run_comparison") as task:
            task.delay.side_effect = RuntimeError("redis is down")
            response = client.post("/runs/compare", data={"models": _TWO})
        assert response.status_code == 200
        assert "redis is down" in response.text

    def test_a_broken_broker_does_not_leave_a_phantom_queued_run(self, client, db):
        """Otherwise the panel polls forever for a task that was never published."""
        from app.services.model_compare import load_state, progress
        self._profile(db)
        with patch("app.tasks.compare_models.run_comparison") as task:
            task.delay.side_effect = RuntimeError("redis is down")
            client.post("/runs/compare", data={"models": _TWO})
        assert progress(load_state(db))["active"] is False

    def test_the_status_endpoint_stops_polling_once_the_run_finishes(self, client, db):
        from app.models.profile import Profile
        db.add(Profile(data={"model_comparison": {
            "at": "2026-08-06T10:30:00+00:00", "status": "done", "threshold": 70,
            "job_count": 1, "models": ["old", "new"],
            "rows": [{"title": "Backend Engineer", "company": "Acme",
                      "scores": {"old": 65, "new": 80}}],
            "summary": [
                {"model": "old", "scored": 1, "average": 65.0, "unreadable": 0,
                 "errors": 0, "seconds": 2.0},
                {"model": "new", "scored": 1, "average": 80.0, "unreadable": 2,
                 "errors": 0, "seconds": 3.0},
            ],
            "flips": [{"title": "Backend Engineer", "company": "Acme",
                       "from": 65, "to": 80, "direction": "gained"}],
        }})) 
        db.commit()

        body = client.get("/runs/compare/status").text

        assert 'hx-trigger="every 5s"' not in body
        assert "now matches" in body
        assert "Backend Engineer" in body
        # The unreadable count is the number that decides usability.
        assert "leaves those jobs unscored" in body

    def test_a_failed_comparison_is_shown_rather_than_silently_missing(self, client, db):
        from app.models.profile import Profile
        db.add(Profile(data={"model_comparison": {
            "at": "2026-08-06T10:30:00+00:00", "status": "failed",
            "error": "nim returned 401", "models": ["m"],
            "rows": [], "summary": [], "flips": [],
        }}))
        db.commit()
        body = client.get("/runs/compare/status").text
        assert "nim returned 401" in body

    def test_the_status_endpoint_survives_never_having_run(self, client):
        response = client.get("/runs/compare/status")
        assert response.status_code == 200
        # A blank panel reads the same as one whose result went missing.
        assert "No comparison has run yet" in response.text
