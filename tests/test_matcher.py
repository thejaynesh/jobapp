import json
import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture
def profile_data():
    return {
        "name": "Jane Doe",
        "target_roles": ["Backend Engineer", "Software Engineer", "Platform Engineer"],
        "target_locations": ["Remote", "New York"],
        "excluded_companies": ["BadCorp", "SkipThis Inc"],
        "min_match_score": 70,
        "skills": {
            "languages": ["Python", "Go", "TypeScript"],
            "frameworks": ["FastAPI", "Django", "React"],
            "tools": ["Docker", "Kubernetes", "Redis"],
            "clouds": ["AWS", "GCP"],
        },
        "narrative": {
            "summary": "Experienced backend engineer with 5 years in distributed systems.",
        },
        "experience": [
            {"title": "Senior Engineer", "company": "Acme", "years": 3},
        ],
    }


@pytest.fixture
def mock_job():
    job = MagicMock()
    job.title = "Backend Engineer"
    job.company = "GoodCorp"
    job.description = (
        "We are looking for a Backend Engineer skilled in Python, FastAPI, Docker, "
        "Redis, Kubernetes, AWS, Go and TypeScript. Experience with Django is a plus."
    )
    job.status = None
    job.keyword_score = None
    job.llm_score = None
    job.llm_reasoning = None
    job.matched_skills = None
    job.missing_skills = None
    return job


# ---------------------------------------------------------------------------
# Task 1 — keyword filter helpers
# ---------------------------------------------------------------------------

class TestFlattenSkills:
    def test_flattens_all_categories(self, profile_data):
        from app.services.matcher import _flatten_skills
        skills = _flatten_skills(profile_data["skills"])
        assert "Python" in skills
        assert "FastAPI" in skills
        assert "Docker" in skills
        assert "AWS" in skills
        assert len(skills) == 11

    def test_handles_missing_categories(self):
        from app.services.matcher import _flatten_skills
        skills = _flatten_skills({"languages": ["Python"]})
        assert skills == ["Python"]

    def test_empty_skills_dict(self):
        from app.services.matcher import _flatten_skills
        assert _flatten_skills({}) == []


class TestTitleMatchesRoles:
    def test_exact_match(self):
        from app.services.matcher import _title_matches_roles
        assert _title_matches_roles("Backend Engineer", ["Backend Engineer"]) is True

    def test_fuzzy_match_above_threshold(self):
        from app.services.matcher import _title_matches_roles
        assert _title_matches_roles("backend engineer", ["Backend Engineer"]) is True

    def test_substring_match_role_in_title(self):
        from app.services.matcher import _title_matches_roles
        assert _title_matches_roles("Senior Backend Engineer", ["Backend Engineer"]) is True

    def test_no_match(self):
        from app.services.matcher import _title_matches_roles
        assert _title_matches_roles("Marketing Manager", ["Backend Engineer", "Software Engineer"]) is False

    def test_empty_roles(self):
        from app.services.matcher import _title_matches_roles
        assert _title_matches_roles("Backend Engineer", []) is False

    def test_partial_fuzzy_match(self):
        from app.services.matcher import _title_matches_roles
        assert _title_matches_roles("Softwre Engineer", ["Software Engineer"]) is True


class TestCountSkillMatches:
    def test_counts_present_skills(self, profile_data):
        from app.services.matcher import _count_skill_matches, _flatten_skills
        skills = _flatten_skills(profile_data["skills"])
        desc = "We need Python and FastAPI and Docker skills."
        count = _count_skill_matches(desc, skills)
        assert count == 3

    def test_case_insensitive(self):
        from app.services.matcher import _count_skill_matches
        count = _count_skill_matches("python FASTAPI docker", ["Python", "FastAPI", "Docker"])
        assert count == 3

    def test_no_skills_in_desc(self, profile_data):
        from app.services.matcher import _count_skill_matches, _flatten_skills
        skills = _flatten_skills(profile_data["skills"])
        count = _count_skill_matches("We need Java and Spring Boot.", skills)
        assert count == 0


class TestKeywordFilter:
    def test_passes_all_criteria(self, mock_job, profile_data):
        from app.services.matcher import keyword_filter
        passes, score = keyword_filter(mock_job, profile_data)
        assert passes is True
        assert score > 0.0

    def test_fails_title_mismatch(self, mock_job, profile_data):
        from app.services.matcher import keyword_filter
        mock_job.title = "Marketing Manager"
        passes, score = keyword_filter(mock_job, profile_data)
        assert passes is False
        assert score == 0.0

    def test_fails_excluded_company(self, mock_job, profile_data):
        from app.services.matcher import keyword_filter
        mock_job.company = "BadCorp"
        passes, score = keyword_filter(mock_job, profile_data)
        assert passes is False
        assert score == 0.0

    def test_fails_too_few_skills(self, mock_job, profile_data):
        from app.services.matcher import keyword_filter
        mock_job.description = "We need a Backend Engineer."
        passes, score = keyword_filter(mock_job, profile_data)
        assert passes is False

    def test_score_is_fraction_of_skills(self, mock_job, profile_data):
        from app.services.matcher import keyword_filter, _flatten_skills
        skills = _flatten_skills(profile_data["skills"])
        passes, score = keyword_filter(mock_job, profile_data)
        assert passes is True
        # description has Python, FastAPI, Docker, Redis, Kubernetes, AWS, Go, TypeScript, Django = 9 of 11
        expected_score = 9 / len(skills)
        assert abs(score - expected_score) < 0.01

    def test_excluded_company_case_insensitive(self, mock_job, profile_data):
        from app.services.matcher import keyword_filter
        mock_job.company = "badcorp"
        passes, score = keyword_filter(mock_job, profile_data)
        assert passes is False


# ---------------------------------------------------------------------------
# Task 2 — prompt builder + response parser
# ---------------------------------------------------------------------------

class TestBuildMatchPrompt:
    def test_contains_candidate_name(self, profile_data, mock_job):
        from app.services.matcher import _build_match_prompt
        messages = _build_match_prompt(mock_job, profile_data)
        full_text = " ".join(m["content"] for m in messages)
        assert "Jane Doe" in full_text

    def test_contains_job_title_and_company(self, profile_data, mock_job):
        from app.services.matcher import _build_match_prompt
        messages = _build_match_prompt(mock_job, profile_data)
        full_text = " ".join(m["content"] for m in messages)
        assert "Backend Engineer" in full_text
        assert "GoodCorp" in full_text

    def test_contains_skills(self, profile_data, mock_job):
        from app.services.matcher import _build_match_prompt
        messages = _build_match_prompt(mock_job, profile_data)
        full_text = " ".join(m["content"] for m in messages)
        assert "Python" in full_text

    def test_contains_narrative_summary(self, profile_data, mock_job):
        from app.services.matcher import _build_match_prompt
        messages = _build_match_prompt(mock_job, profile_data)
        full_text = " ".join(m["content"] for m in messages)
        assert "distributed systems" in full_text

    def test_returns_list_of_dicts_with_role_content(self, profile_data, mock_job):
        from app.services.matcher import _build_match_prompt
        messages = _build_match_prompt(mock_job, profile_data)
        assert isinstance(messages, list)
        assert len(messages) >= 1
        for msg in messages:
            assert "role" in msg
            assert "content" in msg


class TestParseLlmResponse:
    def test_parses_clean_json(self):
        from app.services.matcher import _parse_llm_response
        raw = json.dumps({
            "score": 85,
            "reasoning": "Great fit.",
            "matched_skills": ["Python", "FastAPI"],
            "missing_skills": ["Rust"],
            "seniority_fit": True,
        })
        result = _parse_llm_response(raw)
        assert result["score"] == 85
        assert result["reasoning"] == "Great fit."
        assert "Python" in result["matched_skills"]
        assert result["seniority_fit"] is True

    def test_strips_markdown_code_block(self):
        from app.services.matcher import _parse_llm_response
        raw = '```json\n{"score": 72, "reasoning": "OK", "matched_skills": [], "missing_skills": [], "seniority_fit": false}\n```'
        result = _parse_llm_response(raw)
        assert result["score"] == 72

    def test_strips_plain_code_block(self):
        from app.services.matcher import _parse_llm_response
        raw = '```\n{"score": 60, "reasoning": "Meh", "matched_skills": [], "missing_skills": [], "seniority_fit": false}\n```'
        result = _parse_llm_response(raw)
        assert result["score"] == 60

    def test_returns_zero_score_on_parse_failure(self):
        from app.services.matcher import _parse_llm_response
        result = _parse_llm_response("This is not JSON at all.")
        assert result["score"] == 0
        assert "Parse error" in result["reasoning"]
        assert result["matched_skills"] == []
        assert result["missing_skills"] == []

    def test_returns_zero_score_on_empty_string(self):
        from app.services.matcher import _parse_llm_response
        result = _parse_llm_response("")
        assert result["score"] == 0

    def test_handles_extra_whitespace_around_json(self):
        from app.services.matcher import _parse_llm_response
        raw = '  \n  {"score": 90, "reasoning": "Excellent", "matched_skills": ["Go"], "missing_skills": [], "seniority_fit": true}  \n  '
        result = _parse_llm_response(raw)
        assert result["score"] == 90


# ---------------------------------------------------------------------------
# Task 3 — llm_score_job
# ---------------------------------------------------------------------------

class TestLlmScoreJob:
    def test_returns_parsed_score(self, mock_job, profile_data):
        from app.services.matcher import llm_score_job
        mock_response = json.dumps({
            "score": 88,
            "reasoning": "Strong Python and FastAPI skills match.",
            "matched_skills": ["Python", "FastAPI"],
            "missing_skills": ["Rust"],
            "seniority_fit": True,
        })
        with patch("app.services.matcher.chat_completion", return_value=mock_response):
            result = llm_score_job(mock_job, profile_data, "fake-key", "http://fake", "fake-model")
        assert result["score"] == 88
        assert "Python" in result["matched_skills"]

    def test_returns_zero_on_llm_failure(self, mock_job, profile_data):
        from app.services.matcher import llm_score_job
        with patch("app.services.matcher.chat_completion", side_effect=Exception("LLM unavailable")):
            result = llm_score_job(mock_job, profile_data, "fake-key", "http://fake", "fake-model")
        assert result["score"] == 0

    def test_passes_correct_args_to_chat_completion(self, mock_job, profile_data):
        from app.services.matcher import llm_score_job
        mock_response = json.dumps({
            "score": 75, "reasoning": "OK", "matched_skills": [], "missing_skills": [], "seniority_fit": False,
        })
        with patch("app.services.matcher.chat_completion", return_value=mock_response) as mock_cc:
            llm_score_job(mock_job, profile_data, "my-api-key", "http://base-url", "my-model")
        mock_cc.assert_called_once()
        _, kwargs = mock_cc.call_args
        assert kwargs.get("api_key") == "my-api-key"
        assert kwargs.get("base_url") == "http://base-url"
        assert kwargs.get("model") == "my-model"


# ---------------------------------------------------------------------------
# Task 4 — match_job + match_all_new_jobs
# ---------------------------------------------------------------------------

class TestMatchJob:
    def test_sets_filtered_out_on_keyword_fail(self, mock_job, profile_data):
        from app.services.matcher import match_job
        from app.models.job import JobStatus
        mock_job.title = "Marketing Manager"
        db = MagicMock()
        result = match_job(db, mock_job, profile_data, "key", "url", "model")
        assert result == "filtered_out"
        assert mock_job.status == JobStatus.filtered_out
        assert mock_job.keyword_score == 0.0
        assert mock_job.llm_score is None

    def test_sets_matched_when_score_above_threshold(self, mock_job, profile_data):
        from app.services.matcher import match_job
        from app.models.job import JobStatus
        db = MagicMock()
        llm_result = {"score": 85, "reasoning": "Great fit.", "matched_skills": ["Python"], "missing_skills": [], "seniority_fit": True}
        with patch("app.services.matcher.llm_score_job", return_value=llm_result):
            result = match_job(db, mock_job, profile_data, "key", "url", "model")
        assert result == "matched"
        assert mock_job.status == JobStatus.matched
        assert mock_job.llm_score == 85
        assert mock_job.llm_reasoning == "Great fit."

    def test_sets_filtered_out_when_score_below_threshold(self, mock_job, profile_data):
        from app.services.matcher import match_job
        from app.models.job import JobStatus
        db = MagicMock()
        llm_result = {"score": 30, "reasoning": "Weak.", "matched_skills": [], "missing_skills": ["Rust"], "seniority_fit": False}
        with patch("app.services.matcher.llm_score_job", return_value=llm_result):
            result = match_job(db, mock_job, profile_data, "key", "url", "model")
        assert result == "filtered_out"
        assert mock_job.status == JobStatus.filtered_out
        # 30 raw minus the 15-point seniority-mismatch penalty
        assert mock_job.llm_score == 15

    def test_saves_matched_and_missing_skills(self, mock_job, profile_data):
        from app.services.matcher import match_job
        db = MagicMock()
        llm_result = {"score": 90, "reasoning": "Excellent.", "matched_skills": ["Python", "Go"], "missing_skills": ["Rust"], "seniority_fit": True}
        with patch("app.services.matcher.llm_score_job", return_value=llm_result):
            match_job(db, mock_job, profile_data, "key", "url", "model")
        assert mock_job.matched_skills == ["Python", "Go"]
        assert mock_job.missing_skills == ["Rust"]

    def test_uses_profile_min_score_over_settings(self, mock_job, profile_data):
        from app.services.matcher import match_job
        from app.models.job import JobStatus
        db = MagicMock()
        llm_result = {"score": 65, "reasoning": "Below threshold.", "matched_skills": [], "missing_skills": [], "seniority_fit": True}
        with patch("app.services.matcher.llm_score_job", return_value=llm_result):
            match_job(db, mock_job, profile_data, "key", "url", "model")
        assert mock_job.status == JobStatus.filtered_out

    def test_uses_settings_min_score_when_not_in_profile(self, mock_job, profile_data):
        from app.services.matcher import match_job
        from app.models.job import JobStatus
        profile_no_min = {k: v for k, v in profile_data.items() if k != "min_match_score"}
        db = MagicMock()
        llm_result = {"score": 80, "reasoning": "Good.", "matched_skills": ["Python"], "missing_skills": [], "seniority_fit": True}
        with patch("app.services.matcher.llm_score_job", return_value=llm_result):
            with patch("app.services.matcher.settings") as mock_settings:
                mock_settings.MIN_MATCH_SCORE = 75
                mock_settings.MIN_KEYWORD_SKILLS = 2
                match_job(db, mock_job, profile_no_min, "key", "url", "model")
        assert mock_job.status == JobStatus.matched

    def test_rate_limit_leaves_job_as_new(self, mock_job, profile_data):
        from app.services.matcher import match_job
        from app.models.job import JobStatus
        from openai import RateLimitError
        mock_job.status = JobStatus.new
        db = MagicMock()
        exc = RateLimitError("rate limited", response=MagicMock(status_code=429), body={})
        with patch("app.services.matcher.llm_score_job", side_effect=exc):
            result = match_job(db, mock_job, profile_data, "key", "url", "model")
        assert result == "rate_limited"
        assert mock_job.status == JobStatus.new


class TestLlmScoreJobRateLimit:
    def test_retries_on_429_and_raises(self, mock_job, profile_data):
        from app.services.matcher import llm_score_job
        from openai import RateLimitError
        exc = RateLimitError("rate limited", response=MagicMock(status_code=429), body={})
        with patch("app.services.matcher.chat_completion", side_effect=exc):
            with patch("app.services.matcher.time.sleep"):
                with pytest.raises(RateLimitError):
                    llm_score_job(mock_job, profile_data, "key", "url", "model")

    def test_retries_correct_number_of_times(self, mock_job, profile_data):
        from app.services.matcher import llm_score_job, _retry_delays
        from openai import RateLimitError
        exc = RateLimitError("rate limited", response=MagicMock(status_code=429), body={})
        with patch("app.services.matcher.chat_completion", side_effect=exc) as mock_cc:
            with patch("app.services.matcher.time.sleep"):
                with pytest.raises(RateLimitError):
                    llm_score_job(mock_job, profile_data, "key", "url", "model")
        assert mock_cc.call_count == len(_retry_delays()) + 1

    def test_succeeds_on_retry_after_429(self, mock_job, profile_data):
        import json as json_mod
        from app.services.matcher import llm_score_job
        from openai import RateLimitError
        exc = RateLimitError("rate limited", response=MagicMock(status_code=429), body={})
        success = json_mod.dumps({"score": 80, "reasoning": "ok", "matched_skills": [], "missing_skills": [], "seniority_fit": True})
        with patch("app.services.matcher.chat_completion", side_effect=[exc, success]):
            with patch("app.services.matcher.time.sleep"):
                result = llm_score_job(mock_job, profile_data, "key", "url", "model")
        assert result["score"] == 80

    def test_non_rate_limit_error_returns_zero_score(self, mock_job, profile_data):
        from app.services.matcher import llm_score_job
        with patch("app.services.matcher.chat_completion", side_effect=Exception("network error")):
            result = llm_score_job(mock_job, profile_data, "key", "url", "model")
        assert result["score"] == 0


class TestMatchAllNewJobs:
    def _make_mock_profile(self, profile_data):
        profile = MagicMock()
        profile.data = profile_data
        return profile

    def test_processes_all_new_jobs(self, profile_data):
        from app.services.matcher import match_all_new_jobs
        db = MagicMock()
        job1 = MagicMock()
        job2 = MagicMock()
        mock_profile = self._make_mock_profile(profile_data)
        db.query.return_value.filter.return_value.all.return_value = [job1, job2]
        db.query.return_value.first.return_value = mock_profile
        with patch("app.services.matcher.match_job", return_value="matched"):
            result = match_all_new_jobs(db)
        assert result["processed"] == 2

    def test_returns_zero_when_no_new_jobs(self, profile_data):
        from app.services.matcher import match_all_new_jobs
        db = MagicMock()
        mock_profile = self._make_mock_profile(profile_data)
        db.query.return_value.filter.return_value.all.return_value = []
        db.query.return_value.first.return_value = mock_profile
        with patch("app.services.matcher.match_job") as mock_mj:
            result = match_all_new_jobs(db)
        mock_mj.assert_not_called()
        assert result["processed"] == 0

    def test_commits_after_each_job(self, profile_data):
        from app.services.matcher import match_all_new_jobs
        db = MagicMock()
        job1 = MagicMock()
        mock_profile = self._make_mock_profile(profile_data)
        db.query.return_value.filter.return_value.all.return_value = [job1]
        db.query.return_value.first.return_value = mock_profile
        with patch("app.services.matcher.match_job", return_value="matched"):
            match_all_new_jobs(db)
        db.commit.assert_called()

    def test_counts_rate_limited_separately(self, profile_data):
        from app.services.matcher import match_all_new_jobs
        db = MagicMock()
        mock_profile = self._make_mock_profile(profile_data)
        db.query.return_value.filter.return_value.all.return_value = [MagicMock(), MagicMock()]
        db.query.return_value.first.return_value = mock_profile
        with patch("app.services.matcher.match_job", return_value="rate_limited"):
            with patch("app.services.matcher.time.sleep"):
                result = match_all_new_jobs(db)
        assert result["rate_limited"] == 2
        assert result["filtered_out"] == 0
        assert result["matched"] == 0

    def test_sleeps_between_llm_calls(self, profile_data):
        from app.services.matcher import match_all_new_jobs
        db = MagicMock()
        mock_profile = self._make_mock_profile(profile_data)
        # Two jobs that reach the LLM (llm_score is set)
        job1, job2 = MagicMock(), MagicMock()
        job1.llm_score = 85
        job2.llm_score = 42
        db.query.return_value.filter.return_value.all.return_value = [job1, job2]
        db.query.return_value.first.return_value = mock_profile
        with patch("app.services.matcher.match_job", return_value="matched"):
            with patch("app.services.matcher.time.sleep") as mock_sleep:
                match_all_new_jobs(db)
        assert mock_sleep.call_count == 2

    def test_no_sleep_for_keyword_filtered_jobs(self, profile_data):
        from app.services.matcher import match_all_new_jobs
        db = MagicMock()
        mock_profile = self._make_mock_profile(profile_data)
        # Job that was keyword-filtered (llm_score stays None)
        job = MagicMock()
        job.llm_score = None
        db.query.return_value.filter.return_value.all.return_value = [job]
        db.query.return_value.first.return_value = mock_profile
        with patch("app.services.matcher.match_job", return_value="filtered_out"):
            with patch("app.services.matcher.time.sleep") as mock_sleep:
                match_all_new_jobs(db)
        mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# Task 5 — Celery match_jobs task
# ---------------------------------------------------------------------------

class TestMatchJobsTask:
    def test_task_calls_match_all_new_jobs(self):
        from app.tasks.match import match_jobs
        mock_db = MagicMock()
        with patch("app.tasks.match.match_all_new_jobs") as mock_man:
            with patch("app.tasks.match.SessionLocal", return_value=mock_db):
                mock_man.return_value = {"processed": 3, "matched": 2, "filtered_out": 1, "errors": 0}
                result = match_jobs()
        mock_man.assert_called_once_with(mock_db)
        assert result["processed"] == 3

    def test_task_returns_summary(self):
        from app.tasks.match import match_jobs
        mock_db = MagicMock()
        with patch("app.tasks.match.match_all_new_jobs") as mock_man:
            with patch("app.tasks.match.SessionLocal", return_value=mock_db):
                mock_man.return_value = {"processed": 0, "matched": 0, "filtered_out": 0, "errors": 0}
                result = match_jobs()
        assert "processed" in result
        assert "matched" in result

    def test_task_is_celery_task(self):
        from app.tasks.match import match_jobs
        assert hasattr(match_jobs, "delay")
        assert hasattr(match_jobs, "apply_async")

    def test_task_closes_db_session_on_error(self):
        from app.tasks.match import match_jobs
        mock_db = MagicMock()
        with patch("app.tasks.match.match_all_new_jobs", side_effect=Exception("DB crash")):
            with patch("app.tasks.match.SessionLocal", return_value=mock_db):
                result = match_jobs()
        mock_db.close.assert_called_once()
        assert result.get("errors", 0) >= 1


# ---------------------------------------------------------------------------
# Task 6 — integration tests
# ---------------------------------------------------------------------------

class TestFetchMatchIntegration:
    def test_full_pipeline_keyword_pass_llm_match(self, profile_data):
        from app.services.matcher import match_job
        from app.models.job import JobStatus
        job = MagicMock()
        job.title = "Backend Engineer"
        job.company = "GoodCorp"
        job.description = "Python FastAPI Docker Redis Kubernetes AWS Go TypeScript Django backend."
        db = MagicMock()
        llm_result = {"score": 82, "reasoning": "Strong backend skills.", "matched_skills": ["Python", "FastAPI"], "missing_skills": [], "seniority_fit": True}
        with patch("app.services.matcher.llm_score_job", return_value=llm_result):
            match_job(db, job, profile_data, "key", "url", "model")
        assert job.status == JobStatus.matched
        assert job.llm_score == 82

    def test_full_pipeline_keyword_fail_no_llm(self, profile_data):
        from app.services.matcher import match_job
        from app.models.job import JobStatus
        job = MagicMock()
        job.title = "Sales Executive"
        job.company = "GoodCorp"
        job.description = "Drive revenue growth and manage key accounts."
        db = MagicMock()
        with patch("app.services.matcher.llm_score_job") as mock_llm:
            match_job(db, job, profile_data, "key", "url", "model")
        mock_llm.assert_not_called()
        assert job.status == JobStatus.filtered_out

    def test_match_task_in_celery_includes(self):
        from app.celery_app import celery_app
        import app.tasks.match  # noqa — registers task
        assert "app.tasks.match" in celery_app.conf.include
