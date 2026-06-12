# Job Matching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the 2-stage job matching pipeline — keyword filter + LLM scoring — with a Celery task that processes all new jobs.

**Architecture:** Two-stage pipeline per job: keyword filter (fast, free) then LLM score (NVIDIA NIM). Celery task processes all status=new jobs. Triggered automatically after fetch_jobs via .delay() call.

**Tech Stack:** difflib (fuzzy title match), openai SDK (LLM), Celery, SQLAlchemy, PostgreSQL

**Session import note:** Use `from app.database import SessionLocal` — NOT `app.db.session`.
**Test command:** `docker compose run --rm web pytest ...` — NOT `docker compose run --rm test`.

---

## File Map

Create:
- `app/services/matcher.py` — all matching functions
- `app/tasks/match.py` — Celery task
- `tests/test_matcher.py` — ~49 tests

Modify:
- `app/celery_app.py` — add `app.tasks.match` to include list
- `app/tasks/fetch.py` — call `match_jobs.delay()` after fetch

---

## Task 1: Keyword Filter + Tests

**Files:**
- Create: `app/services/matcher.py`
- Create: `tests/test_matcher.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_matcher.py`:

```python
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


class TestFlattenSkills:
    def test_flattens_all_categories(self, profile_data):
        from app.services.matcher import _flatten_skills
        skills = _flatten_skills(profile_data["skills"])
        assert "Python" in skills
        assert "FastAPI" in skills
        assert "Docker" in skills
        assert "AWS" in skills
        assert len(skills) == 10

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
        # description has Python, FastAPI, Docker, Redis, Kubernetes, AWS, Go, TypeScript, Django = 9 of 10
        expected_score = 9 / len(skills)
        assert abs(score - expected_score) < 0.01

    def test_excluded_company_case_insensitive(self, mock_job, profile_data):
        from app.services.matcher import keyword_filter
        mock_job.company = "badcorp"
        passes, score = keyword_filter(mock_job, profile_data)
        assert passes is False
```

- [ ] **Step 2: Run tests (expect failure)**

```bash
docker compose run --rm web pytest tests/test_matcher.py::TestFlattenSkills tests/test_matcher.py::TestTitleMatchesRoles tests/test_matcher.py::TestCountSkillMatches tests/test_matcher.py::TestKeywordFilter -v --tb=short 2>&1 | head -20
```

Expected: ImportError for `app.services.matcher`

- [ ] **Step 3: Create `app/services/matcher.py`**

```python
from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.llm.client import chat_completion
from app.models.job import Job, JobStatus


def _flatten_skills(skills_data: dict) -> list[str]:
    result: list[str] = []
    for category in ("languages", "frameworks", "tools", "clouds"):
        result.extend(skills_data.get(category, []))
    return result


def _title_matches_roles(title: str, target_roles: list[str]) -> bool:
    title_lower = title.lower()
    for role in target_roles:
        role_lower = role.lower()
        ratio = SequenceMatcher(None, title_lower, role_lower).ratio()
        if ratio >= 0.6:
            return True
        if role_lower in title_lower or title_lower in role_lower:
            return True
    return False


def _count_skill_matches(description: str, skills_flat: list[str]) -> int:
    desc_lower = description.lower()
    return sum(1 for skill in skills_flat if skill.lower() in desc_lower)


def keyword_filter(job: Job, profile_data: dict) -> tuple[bool, float]:
    target_roles: list[str] = profile_data.get("target_roles", [])
    excluded: list[str] = [c.lower() for c in profile_data.get("excluded_companies", [])]
    skills_data: dict = profile_data.get("skills", {})
    skills_flat: list[str] = _flatten_skills(skills_data)
    min_skills: int = settings.MIN_KEYWORD_SKILLS

    if not _title_matches_roles(job.title, target_roles):
        return False, 0.0

    if job.company.lower() in excluded:
        return False, 0.0

    matched_count = _count_skill_matches(job.description, skills_flat)
    score = matched_count / len(skills_flat) if skills_flat else 0.0

    if matched_count < min_skills:
        return False, score

    return True, score
```

- [ ] **Step 4: Run tests**

```bash
docker compose run --rm web pytest tests/test_matcher.py::TestFlattenSkills tests/test_matcher.py::TestTitleMatchesRoles tests/test_matcher.py::TestCountSkillMatches tests/test_matcher.py::TestKeywordFilter -v
```

Expected: 18 passed (3+6+3+6)

- [ ] **Step 5: Commit**

```bash
git add app/services/matcher.py tests/test_matcher.py
git commit -m "feat: job matching — keyword filter functions and tests"
```

---

## Task 2: LLM Prompt Builder + Response Parser + Tests

**Files:**
- Modify: `app/services/matcher.py` (append)
- Modify: `tests/test_matcher.py` (append)

- [ ] **Step 1: Append tests**

Append to `tests/test_matcher.py`:

```python
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
```

- [ ] **Step 2: Run tests (expect failure)**

```bash
docker compose run --rm web pytest tests/test_matcher.py::TestBuildMatchPrompt tests/test_matcher.py::TestParseLlmResponse -v --tb=short 2>&1 | head -20
```

- [ ] **Step 3: Append to `app/services/matcher.py`**

```python
_PROMPT_TEMPLATE = """\
You are a job-fit evaluator. Score how well this candidate matches the job description.

CANDIDATE PROFILE:
Name: {name}
Skills: {skills_summary}
Experience: {experience_summary}
Narrative: {narrative_summary}

JOB DESCRIPTION:
Title: {title}
Company: {company}
{description}

Respond ONLY with valid JSON (no markdown, no explanation):
{{
  "score": <integer 0-100>,
  "reasoning": "<2-3 sentences explaining the match>",
  "matched_skills": ["skill1", "skill2"],
  "missing_skills": ["skill3"],
  "seniority_fit": <true|false>
}}"""


def _build_match_prompt(job: Job, profile_data: dict) -> list[dict[str, str]]:
    skills_data = profile_data.get("skills", {})
    skills_flat = _flatten_skills(skills_data)
    skills_summary = ", ".join(skills_flat) if skills_flat else "Not specified"

    experience = profile_data.get("experience", [])
    if experience:
        exp_parts = [
            f"{e.get('title', '')} at {e.get('company', '')} ({e.get('years', '?')} years)"
            for e in experience
        ]
        experience_summary = "; ".join(exp_parts)
    else:
        experience_summary = "Not specified"

    narrative = profile_data.get("narrative", {})
    if isinstance(narrative, dict):
        narrative_summary = narrative.get("summary", "Not specified")
    else:
        narrative_summary = str(narrative)

    content = _PROMPT_TEMPLATE.format(
        name=profile_data.get("name", "Candidate"),
        skills_summary=skills_summary,
        experience_summary=experience_summary,
        narrative_summary=narrative_summary,
        title=job.title,
        company=job.company,
        description=job.description,
    )
    return [{"role": "user", "content": content}]


def _parse_llm_response(content: str) -> dict[str, Any]:
    _default: dict[str, Any] = {
        "score": 0,
        "reasoning": "Parse error: could not parse LLM response.",
        "matched_skills": [],
        "missing_skills": [],
        "seniority_fit": False,
    }

    if not content or not content.strip():
        return _default

    cleaned = re.sub(r"^```(?:json)?\s*", "", content.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```$", "", cleaned.strip(), flags=re.MULTILINE)
    cleaned = cleaned.strip()

    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return _default

    return {
        "score": int(parsed.get("score", 0)),
        "reasoning": str(parsed.get("reasoning", "")),
        "matched_skills": list(parsed.get("matched_skills", [])),
        "missing_skills": list(parsed.get("missing_skills", [])),
        "seniority_fit": bool(parsed.get("seniority_fit", False)),
    }
```

- [ ] **Step 4: Run tests**

```bash
docker compose run --rm web pytest tests/test_matcher.py::TestBuildMatchPrompt tests/test_matcher.py::TestParseLlmResponse -v
```

Expected: 11 passed (5+6)

- [ ] **Step 5: Commit**

```bash
git add app/services/matcher.py tests/test_matcher.py
git commit -m "feat: LLM prompt builder and response parser"
```

---

## Task 3: `llm_score_job` + Tests

**Files:**
- Modify: `app/services/matcher.py` (append)
- Modify: `tests/test_matcher.py` (append)

- [ ] **Step 1: Append tests**

```python
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
        assert kwargs.get("api_key") == "my-api-key" or "my-api-key" in mock_cc.call_args[0]
```

- [ ] **Step 2: Append to `app/services/matcher.py`**

```python
def llm_score_job(
    job: Job,
    profile_data: dict,
    api_key: str,
    base_url: str,
    model: str,
) -> dict[str, Any]:
    _default: dict[str, Any] = {
        "score": 0,
        "reasoning": "LLM call failed or response could not be parsed.",
        "matched_skills": [],
        "missing_skills": [],
        "seniority_fit": False,
    }
    try:
        messages = _build_match_prompt(job, profile_data)
        raw = chat_completion(
            messages=messages,
            api_key=api_key,
            base_url=base_url,
            model=model,
            temperature=0.1,
            max_tokens=512,
        )
        return _parse_llm_response(raw)
    except Exception:
        return _default
```

- [ ] **Step 3: Run tests**

```bash
docker compose run --rm web pytest tests/test_matcher.py::TestLlmScoreJob -v
```

Expected: 3 passed

- [ ] **Step 4: Commit**

```bash
git add app/services/matcher.py tests/test_matcher.py
git commit -m "feat: llm_score_job function"
```

---

## Task 4: `match_job` + `match_all_new_jobs` + Tests

**Files:**
- Modify: `app/services/matcher.py` (append)
- Modify: `tests/test_matcher.py` (append)

- [ ] **Step 1: Append tests**

```python
class TestMatchJob:
    def test_sets_filtered_out_on_keyword_fail(self, mock_job, profile_data):
        from app.services.matcher import match_job
        from app.models.job import JobStatus
        mock_job.title = "Marketing Manager"
        db = MagicMock()
        match_job(db, mock_job, profile_data, "key", "url", "model")
        assert mock_job.status == JobStatus.filtered_out
        assert mock_job.keyword_score == 0.0
        assert mock_job.llm_score is None

    def test_sets_matched_when_score_above_threshold(self, mock_job, profile_data):
        from app.services.matcher import match_job
        from app.models.job import JobStatus
        db = MagicMock()
        llm_result = {"score": 85, "reasoning": "Great fit.", "matched_skills": ["Python"], "missing_skills": [], "seniority_fit": True}
        with patch("app.services.matcher.llm_score_job", return_value=llm_result):
            match_job(db, mock_job, profile_data, "key", "url", "model")
        assert mock_job.status == JobStatus.matched
        assert mock_job.llm_score == 85
        assert mock_job.llm_reasoning == "Great fit."

    def test_sets_filtered_out_when_score_below_threshold(self, mock_job, profile_data):
        from app.services.matcher import match_job
        from app.models.job import JobStatus
        db = MagicMock()
        llm_result = {"score": 30, "reasoning": "Weak.", "matched_skills": [], "missing_skills": ["Rust"], "seniority_fit": False}
        with patch("app.services.matcher.llm_score_job", return_value=llm_result):
            match_job(db, mock_job, profile_data, "key", "url", "model")
        assert mock_job.status == JobStatus.filtered_out
        assert mock_job.llm_score == 30

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
        # profile has min_match_score=70; score=65 → filtered_out
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
        with patch("app.services.matcher.match_job"):
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
        with patch("app.services.matcher.match_job"):
            match_all_new_jobs(db)
        db.commit.assert_called()
```

- [ ] **Step 2: Append to `app/services/matcher.py`**

```python
def match_job(
    db: Session,
    job: Job,
    profile_data: dict,
    api_key: str,
    base_url: str,
    model: str,
) -> None:
    passes, kw_score = keyword_filter(job, profile_data)
    job.keyword_score = kw_score

    if not passes:
        job.status = JobStatus.filtered_out
        job.llm_score = None
        job.llm_reasoning = None
        job.matched_skills = None
        job.missing_skills = None
        return

    result = llm_score_job(job, profile_data, api_key, base_url, model)
    job.llm_score = result["score"]
    job.llm_reasoning = result["reasoning"]
    job.matched_skills = result.get("matched_skills", [])
    job.missing_skills = result.get("missing_skills", [])

    min_score = profile_data.get("min_match_score", settings.MIN_MATCH_SCORE)
    if result["score"] >= min_score:
        job.status = JobStatus.matched
    else:
        job.status = JobStatus.filtered_out


def match_all_new_jobs(db: Session) -> dict[str, int]:
    from app.models.profile import Profile

    profile = db.query(Profile).first()
    if profile is None:
        return {"processed": 0, "matched": 0, "filtered_out": 0, "errors": 0}

    profile_data: dict = profile.data or {}
    api_key: str = settings.NVIDIA_NIM_API_KEY
    base_url: str = settings.NVIDIA_NIM_BASE_URL
    model: str = settings.NVIDIA_NIM_MODEL

    jobs = db.query(Job).filter(Job.status == JobStatus.new).all()
    counts = {"processed": 0, "matched": 0, "filtered_out": 0, "errors": 0}

    for job in jobs:
        try:
            match_job(db, job, profile_data, api_key, base_url, model)
            db.commit()
            counts["processed"] += 1
            if job.status == JobStatus.matched:
                counts["matched"] += 1
            else:
                counts["filtered_out"] += 1
        except Exception as exc:
            db.rollback()
            counts["errors"] += 1

    return counts
```

- [ ] **Step 3: Run tests**

```bash
docker compose run --rm web pytest tests/test_matcher.py::TestMatchJob tests/test_matcher.py::TestMatchAllNewJobs -v
```

Expected: 9 passed (6+3)

- [ ] **Step 4: Commit**

```bash
git add app/services/matcher.py tests/test_matcher.py
git commit -m "feat: match_job and match_all_new_jobs orchestrators"
```

---

## Task 5: Celery Task `match_jobs` + Tests

**Files:**
- Create: `app/tasks/match.py`
- Modify: `tests/test_matcher.py` (append)

- [ ] **Step 1: Append tests**

```python
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

    def test_task_is_registered_celery_task(self):
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
```

- [ ] **Step 2: Create `app/tasks/match.py`**

```python
import logging
from typing import Any

from app.celery_app import celery_app
from app.database import SessionLocal
from app.services.matcher import match_all_new_jobs

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.match.match_jobs", bind=False)
def match_jobs() -> dict[str, Any]:
    db = SessionLocal()
    try:
        result = match_all_new_jobs(db)
        logger.info(
            "match_jobs completed: processed=%d matched=%d filtered_out=%d errors=%d",
            result.get("processed", 0),
            result.get("matched", 0),
            result.get("filtered_out", 0),
            result.get("errors", 0),
        )
        return result
    except Exception as exc:
        logger.exception("match_jobs task failed: %s", exc)
        return {"processed": 0, "matched": 0, "filtered_out": 0, "errors": 1}
    finally:
        db.close()
```

- [ ] **Step 3: Run tests**

```bash
docker compose run --rm web pytest tests/test_matcher.py::TestMatchJobsTask -v
```

Expected: 4 passed

- [ ] **Step 4: Commit**

```bash
git add app/tasks/match.py tests/test_matcher.py
git commit -m "feat: match_jobs Celery task"
```

---

## Task 6: Chain + celery_app update + integration tests

**Files:**
- Modify: `app/celery_app.py`
- Modify: `app/tasks/fetch.py`
- Modify: `tests/test_matcher.py` (append)

- [ ] **Step 1: Add `app.tasks.match` to `app/celery_app.py` include list**

Change `include=["app.tasks.fetch"]` to `include=["app.tasks.fetch", "app.tasks.match"]`

- [ ] **Step 2: Update `app/tasks/fetch.py` to trigger match after fetch**

Read the file. After `result = fetch_and_save_jobs(db)` and closing the DB, add:

```python
    # Trigger matching pipeline asynchronously
    from app.tasks.match import match_jobs
    match_jobs.delay()
```

This goes inside the try block, after `result = fetch_and_save_jobs(db)` but before `return result`. The DB session must be closed in `finally` before `.delay()` fires a new task.

Actually, structure it as:
```python
@celery_app.task(name="app.tasks.fetch.fetch_jobs", bind=True, max_retries=0)
def fetch_jobs(self) -> dict:
    db = SessionLocal()
    try:
        result = fetch_and_save_jobs(db)
        logger.info(...)
        return result
    except Exception as exc:
        logger.error(...)
        return {"fetched": 0, "inserted": 0, "merged": 0, "skipped": 0}
    finally:
        db.close()
        # Fire match_jobs after fetch regardless of result (new jobs may be inserted)
        try:
            from app.tasks.match import match_jobs
            match_jobs.delay()
        except Exception as exc:
            logger.error("Failed to dispatch match_jobs: %s", exc)
```

- [ ] **Step 3: Append integration tests**

```python
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
        from app.tasks.match import match_jobs  # noqa — registers task
        assert "app.tasks.match" in celery_app.conf.include or "app.tasks.match.match_jobs" in celery_app.tasks
```

- [ ] **Step 4: Run regression + integration tests**

```bash
docker compose run --rm web pytest tests/test_matcher.py -v --tb=short 2>&1 | tail -20
```

Expected: All matcher tests pass

- [ ] **Step 5: Run full suite regression**

```bash
docker compose run --rm web pytest tests/ -q --tb=short 2>&1 | tail -10
```

Expected: All 98 prior tests still pass

- [ ] **Step 6: Commit**

```bash
git add app/celery_app.py app/tasks/fetch.py app/tasks/match.py tests/test_matcher.py
git commit -m "feat: chain fetch_jobs → match_jobs, add integration tests"
```

---

## Task 7: Full test suite pass

**Goal:** All tests green. Target: ~147 tests.

- [ ] **Step 1: Run full suite**

```bash
docker compose run --rm web pytest tests/ -v --tb=short 2>&1 | tail -30
```

- [ ] **Step 2: Common fixes**

**`Job` model missing `keyword_score`/`llm_score` columns** — Check `app/models/job.py`. If missing, these fields need to exist. Read the file. They should already exist (from Plan 01 which set up the full schema per the design spec). If not, add them.

**`db.query.return_value.filter...` mock chain failure** — The `match_all_new_jobs` mock tests use `db.query(Profile).first()` and `db.query(Job).filter(...).all()` on the same mock. The default MagicMock chain handles both. If tests fail, check that `mock_profile` is returned for the first `db.query().first()` call and `[job1, job2]` for `db.query().filter().all()`.

**`chat_completion` signature mismatch** — Verify `app/llm/client.py` accepts `messages`, `api_key`, `base_url`, `model`, `temperature`, `max_tokens` as kwargs. If positional, update `llm_score_job` call accordingly.

- [ ] **Step 3: Confirm count**

```bash
docker compose run --rm web pytest tests/ -q
```

Expected: ~147 passed

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "feat: plan 04 complete — job matching pipeline (147 tests passing)"
```
