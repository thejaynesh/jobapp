# Job Fetching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the complete job fetching pipeline — Tier 1 API adapters (Adzuna, JSearch, Greenhouse, Lever, Ashby), Tier 2 Playwright scrapers (LinkedIn, Indeed, Wellfound, Dice, Handshake), 3-layer deduplication, orchestrator, and Celery Beat task running every 5 hours.

**Architecture:** Modular adapter pattern — each source in its own file returning standard dicts. Orchestrator handles dedup + DB saves. Celery Beat triggers every FETCH_INTERVAL_HOURS (default 5). Playwright scrapers run async inside the orchestrator's sync context via `asyncio.run()`.

**Tech Stack:** httpx (Tier 1 adapters), playwright async API (Tier 2 scrapers), Celery + Redis (task queue), SQLAlchemy + PostgreSQL (DB), ARRAY ops for source_urls dedup.

---

## Standard Job Dict

Every adapter returns `list[dict]` where each item has:

```python
{
    "source": str,            # "adzuna" | "jsearch" | "greenhouse" | "lever" | "ashby" | "linkedin" | "indeed" | "wellfound" | "dice" | "handshake"
    "source_job_id": str | None,
    "title": str,
    "company": str,
    "location": str,
    "is_remote": bool,
    "url": str,
    "description": str,
    "experience_level": str,  # "entry" | "mid" | "senior"
}
```

## File Map

Create:
- `app/services/sources/__init__.py`
- `app/services/sources/base.py` — `parse_experience_level(title, description) -> str`
- `app/services/deduplication.py` — `compute_dedupe_hash`, `find_existing_job`, `merge_or_skip`
- `app/services/sources/adzuna.py`
- `app/services/sources/jsearch.py`
- `app/services/sources/greenhouse.py`
- `app/services/sources/lever.py`
- `app/services/sources/ashby.py`
- `app/services/sources/playwright_base.py`
- `app/services/sources/linkedin.py`
- `app/services/sources/indeed.py`
- `app/services/sources/wellfound.py`
- `app/services/sources/dice.py`
- `app/services/sources/handshake.py`
- `app/services/job_fetcher.py`
- `app/tasks/__init__.py`
- `app/tasks/fetch.py`
- `tests/test_deduplication.py`
- `tests/test_job_sources.py`
- `tests/test_fetch_task.py`

Modify:
- `app/config.py` — 7 new settings
- `app/celery_app.py` — add include + beat_schedule
- `.env.example` — new keys

---

## Task 1: Config additions

**Files:**
- Modify: `app/config.py`
- Modify: `.env.example`

- [ ] **Step 1: Add new settings to `app/config.py`**

Read the current file first. Add these fields to the `Settings` class (after existing fields):

```python
    ADZUNA_APP_ID: str = ""
    ADZUNA_APP_KEY: str = ""
    JSEARCH_API_KEY: str = ""
    LINKEDIN_SESSION_COOKIE: str = ""
    HANDSHAKE_SESSION_COOKIE: str = ""
    GREENHOUSE_COMPANY_SLUGS: str = ""   # comma-separated, e.g. "stripe,airbnb"
    LEVER_COMPANY_SLUGS: str = ""
    ASHBY_COMPANY_SLUGS: str = ""
```

- [ ] **Step 2: Update `.env.example`**

Append:

```
# Job source API keys
ADZUNA_APP_ID=
ADZUNA_APP_KEY=
JSEARCH_API_KEY=
LINKEDIN_SESSION_COOKIE=
HANDSHAKE_SESSION_COOKIE=
GREENHOUSE_COMPANY_SLUGS=   # comma-separated slugs e.g. stripe,airbnb
LEVER_COMPANY_SLUGS=
ASHBY_COMPANY_SLUGS=
```

- [ ] **Step 3: Verify settings load without errors**

```bash
docker compose run --rm web python -c "from app.config import settings; print(settings.ADZUNA_APP_ID)"
```

Expected: prints empty string (no error)

- [ ] **Step 4: Commit**

```bash
git add app/config.py .env.example
git commit -m "feat: add job source config settings"
```

---

## Task 2: Base helper + Deduplication service + tests

**Files:**
- Create: `app/services/sources/__init__.py`
- Create: `app/services/sources/base.py`
- Create: `app/services/deduplication.py`
- Create: `tests/test_deduplication.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_deduplication.py`:

```python
import hashlib
import re

import pytest

from app.models.job import Job, JobStatus


# ---------------------------------------------------------------------------
# parse_experience_level tests
# ---------------------------------------------------------------------------

class TestParseExperienceLevel:
    def test_senior_in_title(self):
        from app.services.sources.base import parse_experience_level
        assert parse_experience_level("Senior Software Engineer", "") == "senior"

    def test_lead_maps_to_senior(self):
        from app.services.sources.base import parse_experience_level
        assert parse_experience_level("Lead Backend Engineer", "") == "senior"

    def test_principal_maps_to_senior(self):
        from app.services.sources.base import parse_experience_level
        assert parse_experience_level("Principal Engineer", "") == "senior"

    def test_junior_in_title(self):
        from app.services.sources.base import parse_experience_level
        assert parse_experience_level("Junior Developer", "") == "entry"

    def test_entry_level_in_description(self):
        from app.services.sources.base import parse_experience_level
        assert parse_experience_level("Software Engineer", "entry level position") == "entry"

    def test_zero_to_two_years(self):
        from app.services.sources.base import parse_experience_level
        assert parse_experience_level("SWE", "0-2 years of experience required") == "entry"

    def test_default_mid(self):
        from app.services.sources.base import parse_experience_level
        assert parse_experience_level("Software Engineer", "Python experience required") == "mid"


# ---------------------------------------------------------------------------
# compute_dedupe_hash tests
# ---------------------------------------------------------------------------

class TestComputeDedupeHash:
    def test_returns_32_char_hex(self):
        from app.services.deduplication import compute_dedupe_hash
        h = compute_dedupe_hash("Stripe", "Software Engineer", "New York, NY")
        assert len(h) == 32
        assert all(c in "0123456789abcdef" for c in h)

    def test_same_inputs_same_hash(self):
        from app.services.deduplication import compute_dedupe_hash
        h1 = compute_dedupe_hash("Stripe", "Software Engineer", "New York, NY")
        h2 = compute_dedupe_hash("Stripe", "Software Engineer", "New York, NY")
        assert h1 == h2

    def test_case_insensitive(self):
        from app.services.deduplication import compute_dedupe_hash
        h1 = compute_dedupe_hash("STRIPE", "SOFTWARE ENGINEER", "NEW YORK, NY")
        h2 = compute_dedupe_hash("stripe", "software engineer", "new york, ny")
        assert h1 == h2

    def test_punctuation_normalized(self):
        from app.services.deduplication import compute_dedupe_hash
        h1 = compute_dedupe_hash("Stripe, Inc.", "Software Engineer", "New York")
        h2 = compute_dedupe_hash("Stripe Inc", "Software Engineer", "New York")
        assert h1 == h2

    def test_different_company_different_hash(self):
        from app.services.deduplication import compute_dedupe_hash
        h1 = compute_dedupe_hash("Stripe", "Software Engineer", "New York")
        h2 = compute_dedupe_hash("Airbnb", "Software Engineer", "New York")
        assert h1 != h2


# ---------------------------------------------------------------------------
# find_existing_job tests
# ---------------------------------------------------------------------------

def _make_job(db, *, company="ACME", title="SWE", location="NYC", url="https://ex.com/1",
              source="adzuna", source_job_id="AZ1",
              dedupe_hash="aabbccdd11223344aabbccdd11223344") -> Job:
    job = Job(
        source=source,
        source_job_id=source_job_id,
        source_urls=[url],
        title=title,
        company=company,
        location=location,
        is_remote=False,
        url=url,
        description="A great job.",
        experience_level="mid",
        status=JobStatus.new,
        dedupe_hash=dedupe_hash,
    )
    db.add(job)
    db.flush()
    return job


class TestFindExistingJob:
    def test_layer1_url_match(self, db):
        from app.services.deduplication import find_existing_job
        job = _make_job(db, url="https://ex.com/1", dedupe_hash="a" * 32)
        result = find_existing_job(db, source="adzuna", url="https://ex.com/1",
                                   source_job_id=None, dedupe_hash="x" * 32)
        assert result is not None
        assert result.id == job.id

    def test_layer2_source_job_id_match(self, db):
        from app.services.deduplication import find_existing_job
        job = _make_job(db, url="https://ex.com/2", source_job_id="JOBID42",
                        dedupe_hash="b" * 32)
        result = find_existing_job(db, source="adzuna", url="https://other.com/999",
                                   source_job_id="JOBID42", dedupe_hash="y" * 32)
        assert result is not None
        assert result.id == job.id

    def test_layer3_dedupe_hash_match(self, db):
        from app.services.deduplication import find_existing_job
        job = _make_job(db, url="https://ex.com/3", source_job_id="ORIG",
                        dedupe_hash="c" * 32)
        result = find_existing_job(db, source="indeed", url="https://indeed.com/999",
                                   source_job_id="DIFF", dedupe_hash="c" * 32)
        assert result is not None
        assert result.id == job.id

    def test_no_match_returns_none(self, db):
        from app.services.deduplication import find_existing_job
        result = find_existing_job(db, source="adzuna", url="https://new.com/1",
                                   source_job_id="NEWID", dedupe_hash="d" * 32)
        assert result is None

    def test_layer2_skipped_when_source_job_id_none(self, db):
        from app.services.deduplication import find_existing_job
        _make_job(db, url="https://ex.com/4", source_job_id="REALID",
                  dedupe_hash="e" * 32)
        result = find_existing_job(db, source="adzuna", url="https://other.com/5",
                                   source_job_id=None, dedupe_hash="f" * 32)
        assert result is None


# ---------------------------------------------------------------------------
# merge_or_skip tests
# ---------------------------------------------------------------------------

class TestMergeOrSkip:
    def test_new_url_appended_to_source_urls(self, db):
        from app.services.deduplication import merge_or_skip
        job = _make_job(db, url="https://ex.com/original", dedupe_hash="f1" * 16)
        merge_or_skip(db, job, new_url="https://crosspost.com/job1",
                      new_description="Short desc.", layer=3)
        db.flush()
        assert "https://crosspost.com/job1" in job.source_urls

    def test_longer_description_replaces_shorter(self, db):
        from app.services.deduplication import merge_or_skip
        job = _make_job(db, url="https://ex.com/a", dedupe_hash="f2" * 16)
        job.description = "Short."
        db.flush()
        merge_or_skip(db, job, new_url="https://new.com/b",
                      new_description="Much longer description with lots of details.",
                      layer=3)
        db.flush()
        assert job.description == "Much longer description with lots of details."

    def test_shorter_description_not_replaced(self, db):
        from app.services.deduplication import merge_or_skip
        job = _make_job(db, url="https://ex.com/c", dedupe_hash="f3" * 16)
        job.description = "A very long existing description with lots of content."
        db.flush()
        merge_or_skip(db, job, new_url="https://new.com/d",
                      new_description="Short.",
                      layer=3)
        db.flush()
        assert "very long" in job.description
```

- [ ] **Step 2: Run tests (expect failure)**

```bash
docker compose run --rm web pytest tests/test_deduplication.py -q
```

Expected: ImportError or ModuleNotFoundError — files not created yet.

- [ ] **Step 3: Create `app/services/sources/__init__.py`**

Empty file:

```python
```

- [ ] **Step 4: Create `app/services/sources/base.py`**

```python
import re


def parse_experience_level(title: str, description: str) -> str:
    """
    Infer seniority from job title and description text.

    Returns "entry", "mid", or "senior".
    """
    text = (title + " " + description).lower()

    senior_patterns = [r"\bsenior\b", r"\bsr\b", r"\blead\b", r"\bprincipal\b",
                       r"\bstaff\b", r"\bdirector\b", r"\bvp\b"]
    if any(re.search(p, text) for p in senior_patterns):
        return "senior"

    entry_patterns = [r"\bjunior\b", r"\bjr\b", r"\bentry[\s\-]level\b",
                      r"\b0[\s\-]?[-–][\s\-]?[12]\s*years?\b", r"\bnew\s+grad\b",
                      r"\bfresh(man|er)?\b"]
    if any(re.search(p, text) for p in entry_patterns):
        return "entry"

    return "mid"
```

- [ ] **Step 5: Create `app/services/deduplication.py`**

```python
import hashlib
import re

from sqlalchemy.orm import Session

from app.models.job import Job


def _normalize(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def compute_dedupe_hash(company: str, title: str, location: str) -> str:
    payload = f"{_normalize(company)}|{_normalize(title)}|{_normalize(location)}"
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def find_existing_job(
    db: Session,
    source: str,
    url: str,
    source_job_id: str | None,
    dedupe_hash: str,
) -> Job | None:
    # Layer 1: URL already in source_urls array
    job = db.query(Job).filter(Job.source_urls.any(url)).first()
    if job:
        return job

    # Layer 2: source + source_job_id match
    if source_job_id:
        job = (
            db.query(Job)
            .filter(Job.source == source, Job.source_job_id == source_job_id)
            .first()
        )
        if job:
            return job

    # Layer 3: content hash (cross-posted job)
    return db.query(Job).filter(Job.dedupe_hash == dedupe_hash).first()


def merge_or_skip(
    db: Session,
    existing: Job,
    new_url: str,
    new_description: str,
    layer: int,
) -> None:
    """Update an existing job when a cross-post is found (layer=3)."""
    if new_url not in existing.source_urls:
        existing.source_urls = existing.source_urls + [new_url]

    if len(new_description) > len(existing.description or ""):
        existing.description = new_description
```

- [ ] **Step 6: Run tests**

```bash
docker compose run --rm web pytest tests/test_deduplication.py -v
```

Expected: 20 passed

- [ ] **Step 7: Commit**

```bash
git add app/services/sources/ app/services/deduplication.py tests/test_deduplication.py
git commit -m "feat: add parse_experience_level helper and 3-layer deduplication service"
```

---

## Task 3: Adzuna adapter + tests

**Files:**
- Create: `app/services/sources/adzuna.py`
- Create: `tests/test_job_sources.py`

Adzuna API: `GET https://api.adzuna.com/v1/api/jobs/{country}/search/1?app_id=X&app_key=Y&what={query}&where={location}&results_per_page=50`

Response JSON: `{"results": [{"id", "title", "company": {"display_name"}, "location": {"display_name"}, "redirect_url", "description", "contract_type"}]}`

- [ ] **Step 1: Write failing tests**

Create `tests/test_job_sources.py`:

```python
from unittest.mock import patch, MagicMock, AsyncMock


# ---------------------------------------------------------------------------
# Adzuna adapter
# ---------------------------------------------------------------------------

class TestAdzunaAdapter:
    def _mock_response(self, jobs_data: list[dict]) -> MagicMock:
        resp = MagicMock()
        resp.json.return_value = {"results": jobs_data}
        resp.raise_for_status = MagicMock()
        return resp

    def test_returns_standard_dicts(self):
        from app.services.sources.adzuna import fetch
        raw = [{
            "id": "AZ123",
            "title": "Senior Python Engineer",
            "company": {"display_name": "Stripe"},
            "location": {"display_name": "New York, NY"},
            "redirect_url": "https://adzuna.com/jobs/AZ123",
            "description": "Build payment systems.",
            "contract_type": "permanent",
        }]
        with patch("httpx.get", return_value=self._mock_response(raw)):
            results = fetch(app_id="ID", app_key="KEY", query="Python", location="New York")
        assert len(results) == 1
        job = results[0]
        assert job["source"] == "adzuna"
        assert job["source_job_id"] == "AZ123"
        assert job["title"] == "Senior Python Engineer"
        assert job["company"] == "Stripe"
        assert job["location"] == "New York, NY"
        assert job["url"] == "https://adzuna.com/jobs/AZ123"
        assert job["experience_level"] == "senior"

    def test_remote_detection_from_location(self):
        from app.services.sources.adzuna import fetch
        raw = [{
            "id": "AZ124",
            "title": "Backend Engineer",
            "company": {"display_name": "Acme"},
            "location": {"display_name": "Remote"},
            "redirect_url": "https://adzuna.com/jobs/AZ124",
            "description": "Remote role.",
            "contract_type": "permanent",
        }]
        with patch("httpx.get", return_value=self._mock_response(raw)):
            results = fetch(app_id="ID", app_key="KEY", query="Engineer", location="Remote")
        assert results[0]["is_remote"] is True

    def test_empty_results(self):
        from app.services.sources.adzuna import fetch
        with patch("httpx.get", return_value=self._mock_response([])):
            results = fetch(app_id="ID", app_key="KEY", query="Python", location="NYC")
        assert results == []

    def test_http_error_returns_empty(self):
        from app.services.sources.adzuna import fetch
        import httpx
        with patch("httpx.get", side_effect=httpx.HTTPError("timeout")):
            results = fetch(app_id="ID", app_key="KEY", query="Python", location="NYC")
        assert results == []
```

- [ ] **Step 2: Run tests (expect failure)**

```bash
docker compose run --rm web pytest tests/test_job_sources.py::TestAdzunaAdapter -q
```

Expected: ImportError

- [ ] **Step 3: Create `app/services/sources/adzuna.py`**

```python
import logging

import httpx

from app.services.sources.base import parse_experience_level

logger = logging.getLogger(__name__)

_BASE = "https://api.adzuna.com/v1/api/jobs"


def fetch(
    app_id: str,
    app_key: str,
    query: str,
    location: str,
    country: str = "us",
    results_per_page: int = 50,
) -> list[dict]:
    url = f"{_BASE}/{country}/search/1"
    params = {
        "app_id": app_id,
        "app_key": app_key,
        "what": query,
        "where": location,
        "results_per_page": results_per_page,
        "content-type": "application/json",
    }
    try:
        resp = httpx.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.error("Adzuna fetch error: %s", exc)
        return []

    jobs = []
    for item in data.get("results", []):
        job_url = item.get("redirect_url", "")
        loc = item.get("location", {}).get("display_name", "")
        title = item.get("title", "")
        desc = item.get("description", "")
        jobs.append({
            "source": "adzuna",
            "source_job_id": str(item.get("id", "")),
            "title": title,
            "company": item.get("company", {}).get("display_name", ""),
            "location": loc,
            "is_remote": "remote" in loc.lower() or "remote" in title.lower(),
            "url": job_url,
            "description": desc,
            "experience_level": parse_experience_level(title, desc),
        })
    return jobs
```

- [ ] **Step 4: Run Adzuna tests**

```bash
docker compose run --rm web pytest tests/test_job_sources.py::TestAdzunaAdapter -v
```

Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add app/services/sources/adzuna.py tests/test_job_sources.py
git commit -m "feat: Adzuna API adapter with tests"
```

---

## Task 4: JSearch adapter + tests

**Files:**
- Create: `app/services/sources/jsearch.py`
- Modify: `tests/test_job_sources.py` (append)

JSearch API (RapidAPI): `GET https://jsearch.p.rapidapi.com/search?query={query+location}&num_pages=1`
Headers: `{"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"}`
Response: `{"data": [{"job_id", "job_title", "employer_name", "job_city", "job_state", "job_country", "job_is_remote", "job_apply_link", "job_description", "job_employment_type"}]}`

- [ ] **Step 1: Append JSearch tests to `tests/test_job_sources.py`**

```python
# ---------------------------------------------------------------------------
# JSearch adapter
# ---------------------------------------------------------------------------

class TestJSearchAdapter:
    def _mock_response(self, jobs_data: list[dict]) -> MagicMock:
        resp = MagicMock()
        resp.json.return_value = {"data": jobs_data}
        resp.raise_for_status = MagicMock()
        return resp

    def test_returns_standard_dicts(self):
        from app.services.sources.jsearch import fetch
        raw = [{
            "job_id": "JS999",
            "job_title": "Backend Engineer",
            "employer_name": "Airbnb",
            "job_city": "San Francisco",
            "job_state": "CA",
            "job_country": "US",
            "job_is_remote": False,
            "job_apply_link": "https://careers.airbnb.com/job/1",
            "job_description": "Build scalable APIs.",
            "job_employment_type": "FULLTIME",
        }]
        with patch("httpx.get", return_value=self._mock_response(raw)):
            results = fetch(api_key="KEY", query="Backend Engineer", location="San Francisco")
        assert len(results) == 1
        job = results[0]
        assert job["source"] == "jsearch"
        assert job["source_job_id"] == "JS999"
        assert job["company"] == "Airbnb"
        assert job["is_remote"] is False
        assert job["url"] == "https://careers.airbnb.com/job/1"

    def test_remote_flag_from_api(self):
        from app.services.sources.jsearch import fetch
        raw = [{
            "job_id": "JS1000",
            "job_title": "SWE",
            "employer_name": "Co",
            "job_city": "",
            "job_state": "",
            "job_country": "US",
            "job_is_remote": True,
            "job_apply_link": "https://co.com/job",
            "job_description": "Remote role.",
            "job_employment_type": "FULLTIME",
        }]
        with patch("httpx.get", return_value=self._mock_response(raw)):
            results = fetch(api_key="KEY", query="SWE", location="Remote")
        assert results[0]["is_remote"] is True

    def test_http_error_returns_empty(self):
        from app.services.sources.jsearch import fetch
        import httpx
        with patch("httpx.get", side_effect=httpx.HTTPError("timeout")):
            results = fetch(api_key="KEY", query="SWE", location="NYC")
        assert results == []
```

- [ ] **Step 2: Create `app/services/sources/jsearch.py`**

```python
import logging

import httpx

from app.services.sources.base import parse_experience_level

logger = logging.getLogger(__name__)

_BASE = "https://jsearch.p.rapidapi.com/search"
_HOST = "jsearch.p.rapidapi.com"


def fetch(
    api_key: str,
    query: str,
    location: str,
    num_pages: int = 1,
) -> list[dict]:
    headers = {"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": _HOST}
    params = {"query": f"{query} in {location}", "num_pages": num_pages}
    try:
        resp = httpx.get(_BASE, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.error("JSearch fetch error: %s", exc)
        return []

    jobs = []
    for item in data.get("data", []):
        title = item.get("job_title", "")
        desc = item.get("job_description", "")
        city = item.get("job_city", "")
        state = item.get("job_state", "")
        loc = ", ".join(filter(None, [city, state])) or item.get("job_country", "")
        jobs.append({
            "source": "jsearch",
            "source_job_id": item.get("job_id"),
            "title": title,
            "company": item.get("employer_name", ""),
            "location": loc,
            "is_remote": bool(item.get("job_is_remote", False)),
            "url": item.get("job_apply_link", ""),
            "description": desc,
            "experience_level": parse_experience_level(title, desc),
        })
    return jobs
```

- [ ] **Step 3: Run JSearch tests**

```bash
docker compose run --rm web pytest tests/test_job_sources.py::TestJSearchAdapter -v
```

Expected: 3 passed

- [ ] **Step 4: Commit**

```bash
git add app/services/sources/jsearch.py tests/test_job_sources.py
git commit -m "feat: JSearch API adapter with tests"
```

---

## Task 5: Greenhouse adapter + tests

**Files:**
- Create: `app/services/sources/greenhouse.py`
- Modify: `tests/test_job_sources.py` (append)

Greenhouse API: `GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true`
Response: `{"jobs": [{"id", "title", "location": {"name"}, "absolute_url", "content", "metadata"}]}`

- [ ] **Step 1: Append Greenhouse tests**

```python
# ---------------------------------------------------------------------------
# Greenhouse adapter
# ---------------------------------------------------------------------------

class TestGreenhouseAdapter:
    def _mock_response(self, jobs_data: list[dict], slug: str) -> MagicMock:
        resp = MagicMock()
        resp.json.return_value = {"jobs": jobs_data}
        resp.raise_for_status = MagicMock()
        return resp

    def test_returns_standard_dicts(self):
        from app.services.sources.greenhouse import fetch
        raw = [{
            "id": 4001,
            "title": "Software Engineer",
            "location": {"name": "San Francisco, CA"},
            "absolute_url": "https://boards.greenhouse.io/stripe/jobs/4001",
            "content": "Build APIs at Stripe.",
        }]
        with patch("httpx.get", return_value=self._mock_response(raw, "stripe")):
            results = fetch(company_slugs=["stripe"])
        assert len(results) == 1
        job = results[0]
        assert job["source"] == "greenhouse"
        assert job["source_job_id"] == "4001"
        assert job["company"] == "stripe"
        assert job["url"] == "https://boards.greenhouse.io/stripe/jobs/4001"

    def test_multiple_slugs_merged(self):
        from app.services.sources.greenhouse import fetch
        raw_stripe = [{"id": 1, "title": "SWE", "location": {"name": "NYC"},
                       "absolute_url": "https://greenhouse.io/stripe/1", "content": "desc"}]
        raw_airbnb = [{"id": 2, "title": "SRE", "location": {"name": "SF"},
                       "absolute_url": "https://greenhouse.io/airbnb/2", "content": "desc"}]
        with patch("httpx.get", side_effect=[
            self._mock_response(raw_stripe, "stripe"),
            self._mock_response(raw_airbnb, "airbnb"),
        ]):
            results = fetch(company_slugs=["stripe", "airbnb"])
        assert len(results) == 2

    def test_failed_slug_skipped(self):
        from app.services.sources.greenhouse import fetch
        import httpx
        raw_ok = [{"id": 1, "title": "SWE", "location": {"name": "NYC"},
                   "absolute_url": "https://greenhouse.io/good/1", "content": "desc"}]
        with patch("httpx.get", side_effect=[
            httpx.HTTPError("404"),
            self._mock_response(raw_ok, "good"),
        ]):
            results = fetch(company_slugs=["bad_slug", "good"])
        assert len(results) == 1
```

- [ ] **Step 2: Create `app/services/sources/greenhouse.py`**

```python
import logging

import httpx

from app.services.sources.base import parse_experience_level

logger = logging.getLogger(__name__)


def fetch(company_slugs: list[str]) -> list[dict]:
    jobs = []
    for slug in company_slugs:
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
        try:
            resp = httpx.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.error("Greenhouse fetch error for slug '%s': %s", slug, exc)
            continue

        for item in data.get("jobs", []):
            title = item.get("title", "")
            desc = item.get("content", "")
            loc = item.get("location", {}).get("name", "")
            jobs.append({
                "source": "greenhouse",
                "source_job_id": str(item.get("id", "")),
                "title": title,
                "company": slug,
                "location": loc,
                "is_remote": "remote" in loc.lower() or "remote" in title.lower(),
                "url": item.get("absolute_url", ""),
                "description": desc,
                "experience_level": parse_experience_level(title, desc),
            })
    return jobs
```

- [ ] **Step 3: Run Greenhouse tests**

```bash
docker compose run --rm web pytest tests/test_job_sources.py::TestGreenhouseAdapter -v
```

Expected: 3 passed

- [ ] **Step 4: Commit**

```bash
git add app/services/sources/greenhouse.py tests/test_job_sources.py
git commit -m "feat: Greenhouse API adapter with tests"
```

---

## Task 6: Lever + Ashby adapters + tests

**Files:**
- Create: `app/services/sources/lever.py`
- Create: `app/services/sources/ashby.py`
- Modify: `tests/test_job_sources.py` (append)

Lever API: `GET https://api.lever.co/v0/postings/{slug}?mode=json`
Response: `[{"id", "text" (title), "categories": {"location", "team"}, "hostedUrl", "descriptionPlain"}]`

Ashby API: `GET https://jobs.ashbyhq.com/api/non-user-facing/posting-board/job-board/jobs`
Params: `{"organizationHostedJobsPageName": slug}`
Response: `{"jobPostings": [{"id", "title", "locationName", "isRemote", "jobUrl", "descriptionHtml"}]}`

- [ ] **Step 1: Append Lever + Ashby tests**

```python
# ---------------------------------------------------------------------------
# Lever adapter
# ---------------------------------------------------------------------------

class TestLeverAdapter:
    def test_returns_standard_dicts(self):
        from app.services.sources.lever import fetch
        raw = [{
            "id": "lever-uuid-001",
            "text": "ML Engineer",
            "categories": {"location": "Remote", "team": "AI"},
            "hostedUrl": "https://jobs.lever.co/openai/lever-uuid-001",
            "descriptionPlain": "Build ML systems.",
        }]
        with patch("httpx.get", return_value=MagicMock(
            json=lambda: raw, raise_for_status=MagicMock()
        )):
            results = fetch(company_slugs=["openai"])
        assert len(results) == 1
        job = results[0]
        assert job["source"] == "lever"
        assert job["source_job_id"] == "lever-uuid-001"
        assert job["title"] == "ML Engineer"
        assert job["is_remote"] is True

    def test_failed_slug_skipped(self):
        from app.services.sources.lever import fetch
        import httpx
        with patch("httpx.get", side_effect=httpx.HTTPError("404")):
            results = fetch(company_slugs=["nonexistent"])
        assert results == []


# ---------------------------------------------------------------------------
# Ashby adapter
# ---------------------------------------------------------------------------

class TestAshbyAdapter:
    def test_returns_standard_dicts(self):
        from app.services.sources.ashby import fetch
        raw = {"jobPostings": [{
            "id": "ashby-001",
            "title": "Staff Engineer",
            "locationName": "New York, NY",
            "isRemote": False,
            "jobUrl": "https://jobs.ashbyhq.com/rippling/ashby-001",
            "descriptionHtml": "<p>Scale infrastructure.</p>",
        }]}
        with patch("httpx.get", return_value=MagicMock(
            json=lambda: raw, raise_for_status=MagicMock()
        )):
            results = fetch(company_slugs=["rippling"])
        assert len(results) == 1
        job = results[0]
        assert job["source"] == "ashby"
        assert job["source_job_id"] == "ashby-001"
        assert job["experience_level"] == "senior"

    def test_failed_slug_skipped(self):
        from app.services.sources.ashby import fetch
        import httpx
        with patch("httpx.get", side_effect=httpx.HTTPError("500")):
            results = fetch(company_slugs=["bad"])
        assert results == []
```

- [ ] **Step 2: Create `app/services/sources/lever.py`**

```python
import logging

import httpx

from app.services.sources.base import parse_experience_level

logger = logging.getLogger(__name__)


def fetch(company_slugs: list[str]) -> list[dict]:
    jobs = []
    for slug in company_slugs:
        url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        try:
            resp = httpx.get(url, timeout=15)
            resp.raise_for_status()
            items = resp.json()
        except Exception as exc:
            logger.error("Lever fetch error for slug '%s': %s", slug, exc)
            continue

        for item in items:
            title = item.get("text", "")
            desc = item.get("descriptionPlain", "")
            loc = item.get("categories", {}).get("location", "")
            jobs.append({
                "source": "lever",
                "source_job_id": item.get("id"),
                "title": title,
                "company": slug,
                "location": loc,
                "is_remote": "remote" in loc.lower() or "remote" in title.lower(),
                "url": item.get("hostedUrl", ""),
                "description": desc,
                "experience_level": parse_experience_level(title, desc),
            })
    return jobs
```

- [ ] **Step 3: Create `app/services/sources/ashby.py`**

```python
import logging
import re

import httpx

from app.services.sources.base import parse_experience_level

logger = logging.getLogger(__name__)

_BASE = "https://jobs.ashbyhq.com/api/non-user-facing/posting-board/job-board/jobs"


def _strip_html(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html).strip()


def fetch(company_slugs: list[str]) -> list[dict]:
    jobs = []
    for slug in company_slugs:
        params = {"organizationHostedJobsPageName": slug}
        try:
            resp = httpx.get(_BASE, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.error("Ashby fetch error for slug '%s': %s", slug, exc)
            continue

        for item in data.get("jobPostings", []):
            title = item.get("title", "")
            desc = _strip_html(item.get("descriptionHtml", ""))
            loc = item.get("locationName", "")
            jobs.append({
                "source": "ashby",
                "source_job_id": item.get("id"),
                "title": title,
                "company": slug,
                "location": loc,
                "is_remote": bool(item.get("isRemote", False)),
                "url": item.get("jobUrl", ""),
                "description": desc,
                "experience_level": parse_experience_level(title, desc),
            })
    return jobs
```

- [ ] **Step 4: Run Lever + Ashby tests**

```bash
docker compose run --rm web pytest tests/test_job_sources.py::TestLeverAdapter tests/test_job_sources.py::TestAshbyAdapter -v
```

Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add app/services/sources/lever.py app/services/sources/ashby.py tests/test_job_sources.py
git commit -m "feat: Lever and Ashby API adapters with tests"
```

---

## Task 7: Playwright base + LinkedIn scraper + tests

**Files:**
- Create: `app/services/sources/playwright_base.py`
- Create: `app/services/sources/linkedin.py`
- Modify: `tests/test_job_sources.py` (append)

- [ ] **Step 1: Append LinkedIn tests**

```python
# ---------------------------------------------------------------------------
# LinkedIn scraper (playwright, mocked)
# ---------------------------------------------------------------------------

class TestLinkedInScraper:
    def _make_mock_card(self, title="SWE", company="Stripe",
                        location="NYC", url="https://linkedin.com/jobs/1") -> MagicMock:
        card = AsyncMock()
        card.query_selector = AsyncMock(side_effect=lambda sel: AsyncMock(
            inner_text=AsyncMock(return_value={
                ".base-search-card__title": title,
                ".base-search-card__subtitle": company,
                ".job-search-card__location": location,
            }.get(sel, "unknown")),
            get_attribute=AsyncMock(return_value=url),
        ))
        return card

    def test_returns_standard_dicts(self):
        import asyncio
        from unittest.mock import patch, AsyncMock, MagicMock

        async def mock_fetch(*args, **kwargs):
            return [{
                "source": "linkedin",
                "source_job_id": None,
                "title": "Software Engineer",
                "company": "Stripe",
                "location": "New York, NY",
                "is_remote": False,
                "url": "https://linkedin.com/jobs/1",
                "description": "",
                "experience_level": "mid",
            }]

        with patch("app.services.sources.linkedin.fetch", side_effect=mock_fetch):
            from app.services.sources.linkedin import fetch
            results = asyncio.run(fetch(
                session_cookie="test_cookie",
                query="Software Engineer",
                location="New York",
            ))

        assert len(results) == 1
        assert results[0]["source"] == "linkedin"
        assert results[0]["company"] == "Stripe"

    def test_empty_on_playwright_error(self):
        import asyncio
        from unittest.mock import patch

        async def raise_error(*args, **kwargs):
            raise RuntimeError("Browser crash")

        with patch("app.services.sources.linkedin._scrape", side_effect=raise_error):
            from app.services.sources import linkedin
            results = asyncio.run(linkedin.fetch(
                session_cookie="cookie",
                query="SWE",
                location="NYC",
            ))
        assert results == []
```

- [ ] **Step 2: Create `app/services/sources/playwright_base.py`**

```python
LAUNCH_OPTIONS = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}


async def safe_inner_text(element, selector: str, default: str = "") -> str:
    try:
        el = await element.query_selector(selector)
        if el:
            return (await el.inner_text()).strip()
    except Exception:
        pass
    return default


async def safe_get_attribute(element, selector: str, attr: str, default: str = "") -> str:
    try:
        el = await element.query_selector(selector)
        if el:
            val = await el.get_attribute(attr)
            return val or default
    except Exception:
        pass
    return default


def is_remote_location(location: str, title: str) -> bool:
    text = (location + " " + title).lower()
    return "remote" in text
```

- [ ] **Step 3: Create `app/services/sources/linkedin.py`**

```python
import logging

from app.services.sources.playwright_base import (
    LAUNCH_OPTIONS,
    is_remote_location,
    safe_get_attribute,
    safe_inner_text,
)

logger = logging.getLogger(__name__)


async def _scrape(session_cookie: str, query: str, location: str) -> list[dict]:
    from playwright.async_api import async_playwright

    url = (
        f"https://www.linkedin.com/jobs/search/"
        f"?keywords={query}&location={location}"
    )
    async with async_playwright() as p:
        browser = await p.chromium.launch(**LAUNCH_OPTIONS)
        context = await browser.new_context()
        await context.add_cookies([{
            "name": "li_at",
            "value": session_cookie,
            "domain": ".linkedin.com",
            "path": "/",
        }])
        page = await context.new_page()
        try:
            await page.goto(url, timeout=30000)
            await page.wait_for_selector(".base-card", timeout=10000)
        except Exception as exc:
            logger.warning("LinkedIn: page load failed: %s", exc)
            await browser.close()
            return []

        cards = await page.query_selector_all(".base-card")
        jobs = []
        for card in cards:
            title = await safe_inner_text(card, ".base-search-card__title")
            company = await safe_inner_text(card, ".base-search-card__subtitle")
            loc = await safe_inner_text(card, ".job-search-card__location")
            job_url = await safe_get_attribute(card, "a.base-card__full-link", "href")
            if not title or not job_url:
                continue
            jobs.append({
                "source": "linkedin",
                "source_job_id": None,
                "title": title,
                "company": company,
                "location": loc,
                "is_remote": is_remote_location(loc, title),
                "url": job_url,
                "description": "",
                "experience_level": "mid",
            })
        await browser.close()
        return jobs


async def fetch(session_cookie: str, query: str, location: str) -> list[dict]:
    try:
        return await _scrape(session_cookie, query, location)
    except Exception as exc:
        logger.error("LinkedIn scraper error: %s", exc)
        return []
```

- [ ] **Step 4: Run LinkedIn tests**

```bash
docker compose run --rm web pytest tests/test_job_sources.py::TestLinkedInScraper -v
```

Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add app/services/sources/playwright_base.py app/services/sources/linkedin.py tests/test_job_sources.py
git commit -m "feat: playwright base and LinkedIn scraper with tests"
```

---

## Task 8: Indeed scraper + tests

**Files:**
- Create: `app/services/sources/indeed.py`
- Modify: `tests/test_job_sources.py` (append)

Indeed URL: `https://www.indeed.com/jobs?q={query}&l={location}`
Selector: `[data-testid="slider_item"]` — job cards
Inner selectors: `.jobTitle span` (title), `.companyName` (company), `.companyLocation` (location), `a.jcs-JobTitle` href (url)

- [ ] **Step 1: Append Indeed tests**

```python
# ---------------------------------------------------------------------------
# Indeed scraper (playwright, mocked)
# ---------------------------------------------------------------------------

class TestIndeedScraper:
    def test_returns_standard_dicts(self):
        import asyncio

        async def mock_scrape(*args, **kwargs):
            return [{
                "source": "indeed",
                "source_job_id": None,
                "title": "Backend Engineer",
                "company": "Meta",
                "location": "Menlo Park, CA",
                "is_remote": False,
                "url": "https://indeed.com/viewjob?jk=abc123",
                "description": "",
                "experience_level": "mid",
            }]

        with patch("app.services.sources.indeed._scrape", side_effect=mock_scrape):
            from app.services.sources.indeed import fetch
            results = asyncio.run(fetch(query="Backend Engineer", location="Menlo Park"))

        assert len(results) == 1
        assert results[0]["source"] == "indeed"

    def test_empty_on_error(self):
        import asyncio

        async def raise_error(*args, **kwargs):
            raise RuntimeError("Timeout")

        with patch("app.services.sources.indeed._scrape", side_effect=raise_error):
            from app.services.sources import indeed
            results = asyncio.run(indeed.fetch(query="SWE", location="NYC"))
        assert results == []
```

- [ ] **Step 2: Create `app/services/sources/indeed.py`**

```python
import logging

from app.services.sources.base import parse_experience_level
from app.services.sources.playwright_base import (
    LAUNCH_OPTIONS,
    is_remote_location,
    safe_get_attribute,
    safe_inner_text,
)

logger = logging.getLogger(__name__)


async def _scrape(query: str, location: str) -> list[dict]:
    from playwright.async_api import async_playwright

    url = f"https://www.indeed.com/jobs?q={query}&l={location}"
    async with async_playwright() as p:
        browser = await p.chromium.launch(**LAUNCH_OPTIONS)
        page = await browser.new_page()
        try:
            await page.goto(url, timeout=30000)
            await page.wait_for_selector('[data-testid="slider_item"]', timeout=10000)
        except Exception as exc:
            logger.warning("Indeed: page load failed: %s", exc)
            await browser.close()
            return []

        cards = await page.query_selector_all('[data-testid="slider_item"]')
        jobs = []
        for card in cards:
            title = await safe_inner_text(card, ".jobTitle span")
            company = await safe_inner_text(card, ".companyName")
            loc = await safe_inner_text(card, ".companyLocation")
            job_url = await safe_get_attribute(card, "a.jcs-JobTitle", "href")
            if not title:
                continue
            jobs.append({
                "source": "indeed",
                "source_job_id": None,
                "title": title,
                "company": company,
                "location": loc,
                "is_remote": is_remote_location(loc, title),
                "url": job_url,
                "description": "",
                "experience_level": parse_experience_level(title, ""),
            })
        await browser.close()
        return jobs


async def fetch(query: str, location: str) -> list[dict]:
    try:
        return await _scrape(query, location)
    except Exception as exc:
        logger.error("Indeed scraper error: %s", exc)
        return []
```

- [ ] **Step 3: Run Indeed tests**

```bash
docker compose run --rm web pytest tests/test_job_sources.py::TestIndeedScraper -v
```

Expected: 2 passed

- [ ] **Step 4: Commit**

```bash
git add app/services/sources/indeed.py tests/test_job_sources.py
git commit -m "feat: Indeed playwright scraper with tests"
```

---

## Task 9: Wellfound + Dice + Handshake scrapers + tests

**Files:**
- Create: `app/services/sources/wellfound.py`
- Create: `app/services/sources/dice.py`
- Create: `app/services/sources/handshake.py`
- Modify: `tests/test_job_sources.py` (append)

Wellfound URL: `https://wellfound.com/jobs?role={query}&location={location}`
Selector: `.styles_component__job` or `.job-listing` (cards)

Dice URL: `https://www.dice.com/jobs?q={query}&location={location}`
Selector: `dhi-job-card` (web component)

Handshake URL: `https://joinhandshake.com/stu/postings?search[query]={query}`
Auth: inject `_handshake_session` cookie
Selector: `.posting-listing-item`

- [ ] **Step 1: Append Wellfound + Dice + Handshake tests**

```python
# ---------------------------------------------------------------------------
# Wellfound, Dice, Handshake scrapers (playwright, mocked)
# ---------------------------------------------------------------------------

class TestWellfoundScraper:
    def test_returns_standard_dicts(self):
        import asyncio

        async def mock_scrape(*args, **kwargs):
            return [{"source": "wellfound", "source_job_id": None,
                     "title": "SWE", "company": "Startup", "location": "Remote",
                     "is_remote": True, "url": "https://wellfound.com/job/1",
                     "description": "", "experience_level": "mid"}]

        with patch("app.services.sources.wellfound._scrape", side_effect=mock_scrape):
            from app.services.sources.wellfound import fetch
            results = asyncio.run(fetch(query="SWE", location="Remote"))
        assert results[0]["source"] == "wellfound"

    def test_empty_on_error(self):
        import asyncio

        async def raise_error(*args, **kwargs):
            raise RuntimeError("Block")

        with patch("app.services.sources.wellfound._scrape", side_effect=raise_error):
            from app.services.sources import wellfound
            results = asyncio.run(wellfound.fetch(query="SWE", location="NYC"))
        assert results == []


class TestDiceScraper:
    def test_returns_standard_dicts(self):
        import asyncio

        async def mock_scrape(*args, **kwargs):
            return [{"source": "dice", "source_job_id": None,
                     "title": "DevOps Engineer", "company": "TechCo",
                     "location": "Austin, TX", "is_remote": False,
                     "url": "https://dice.com/job/1", "description": "",
                     "experience_level": "mid"}]

        with patch("app.services.sources.dice._scrape", side_effect=mock_scrape):
            from app.services.sources.dice import fetch
            results = asyncio.run(fetch(query="DevOps", location="Austin"))
        assert results[0]["source"] == "dice"

    def test_empty_on_error(self):
        import asyncio

        async def raise_error(*args, **kwargs):
            raise RuntimeError("Timeout")

        with patch("app.services.sources.dice._scrape", side_effect=raise_error):
            from app.services.sources import dice
            results = asyncio.run(dice.fetch(query="SWE", location="NYC"))
        assert results == []


class TestHandshakeScraper:
    def test_returns_standard_dicts(self):
        import asyncio

        async def mock_scrape(*args, **kwargs):
            return [{"source": "handshake", "source_job_id": None,
                     "title": "New Grad SWE", "company": "Amazon",
                     "location": "Seattle, WA", "is_remote": False,
                     "url": "https://joinhandshake.com/posting/1", "description": "",
                     "experience_level": "entry"}]

        with patch("app.services.sources.handshake._scrape", side_effect=mock_scrape):
            from app.services.sources.handshake import fetch
            results = asyncio.run(fetch(session_cookie="sess", query="SWE", location=""))
        assert results[0]["source"] == "handshake"

    def test_empty_on_error(self):
        import asyncio

        async def raise_error(*args, **kwargs):
            raise RuntimeError("Login required")

        with patch("app.services.sources.handshake._scrape", side_effect=raise_error):
            from app.services.sources import handshake
            results = asyncio.run(handshake.fetch(session_cookie="s", query="SWE", location=""))
        assert results == []
```

- [ ] **Step 2: Create `app/services/sources/wellfound.py`**

```python
import logging

from app.services.sources.base import parse_experience_level
from app.services.sources.playwright_base import LAUNCH_OPTIONS, is_remote_location, safe_inner_text, safe_get_attribute

logger = logging.getLogger(__name__)


async def _scrape(query: str, location: str) -> list[dict]:
    from playwright.async_api import async_playwright

    url = f"https://wellfound.com/jobs?role={query}&location={location}"
    async with async_playwright() as p:
        browser = await p.chromium.launch(**LAUNCH_OPTIONS)
        page = await browser.new_page()
        try:
            await page.goto(url, timeout=30000)
            await page.wait_for_selector(".styles_component__job", timeout=10000)
        except Exception as exc:
            logger.warning("Wellfound: page load failed: %s", exc)
            await browser.close()
            return []

        cards = await page.query_selector_all(".styles_component__job")
        jobs = []
        for card in cards:
            title = await safe_inner_text(card, "h2")
            company = await safe_inner_text(card, ".styles_company__name")
            loc = await safe_inner_text(card, ".styles_location")
            job_url = await safe_get_attribute(card, "a", "href")
            if not title:
                continue
            jobs.append({
                "source": "wellfound",
                "source_job_id": None,
                "title": title,
                "company": company,
                "location": loc,
                "is_remote": is_remote_location(loc, title),
                "url": job_url,
                "description": "",
                "experience_level": parse_experience_level(title, ""),
            })
        await browser.close()
        return jobs


async def fetch(query: str, location: str) -> list[dict]:
    try:
        return await _scrape(query, location)
    except Exception as exc:
        logger.error("Wellfound scraper error: %s", exc)
        return []
```

- [ ] **Step 3: Create `app/services/sources/dice.py`**

```python
import logging

from app.services.sources.base import parse_experience_level
from app.services.sources.playwright_base import LAUNCH_OPTIONS, is_remote_location, safe_inner_text, safe_get_attribute

logger = logging.getLogger(__name__)


async def _scrape(query: str, location: str) -> list[dict]:
    from playwright.async_api import async_playwright

    url = f"https://www.dice.com/jobs?q={query}&location={location}"
    async with async_playwright() as p:
        browser = await p.chromium.launch(**LAUNCH_OPTIONS)
        page = await browser.new_page()
        try:
            await page.goto(url, timeout=30000)
            await page.wait_for_selector("dhi-job-card", timeout=10000)
        except Exception as exc:
            logger.warning("Dice: page load failed: %s", exc)
            await browser.close()
            return []

        cards = await page.query_selector_all("dhi-job-card")
        jobs = []
        for card in cards:
            title = await safe_inner_text(card, "a.card-title-link")
            company = await safe_inner_text(card, ".card-company")
            loc = await safe_inner_text(card, ".search-result-location")
            job_url = await safe_get_attribute(card, "a.card-title-link", "href")
            if not title:
                continue
            jobs.append({
                "source": "dice",
                "source_job_id": None,
                "title": title,
                "company": company,
                "location": loc,
                "is_remote": is_remote_location(loc, title),
                "url": job_url,
                "description": "",
                "experience_level": parse_experience_level(title, ""),
            })
        await browser.close()
        return jobs


async def fetch(query: str, location: str) -> list[dict]:
    try:
        return await _scrape(query, location)
    except Exception as exc:
        logger.error("Dice scraper error: %s", exc)
        return []
```

- [ ] **Step 4: Create `app/services/sources/handshake.py`**

```python
import logging

from app.services.sources.base import parse_experience_level
from app.services.sources.playwright_base import LAUNCH_OPTIONS, is_remote_location, safe_inner_text, safe_get_attribute

logger = logging.getLogger(__name__)


async def _scrape(session_cookie: str, query: str, location: str) -> list[dict]:
    from playwright.async_api import async_playwright

    url = f"https://joinhandshake.com/stu/postings?search[query]={query}"
    async with async_playwright() as p:
        browser = await p.chromium.launch(**LAUNCH_OPTIONS)
        context = await browser.new_context()
        await context.add_cookies([{
            "name": "_handshake_session",
            "value": session_cookie,
            "domain": "joinhandshake.com",
            "path": "/",
        }])
        page = await context.new_page()
        try:
            await page.goto(url, timeout=30000)
            await page.wait_for_selector(".posting-listing-item", timeout=10000)
        except Exception as exc:
            logger.warning("Handshake: page load failed: %s", exc)
            await browser.close()
            return []

        cards = await page.query_selector_all(".posting-listing-item")
        jobs = []
        for card in cards:
            title = await safe_inner_text(card, ".posting-listing-title")
            company = await safe_inner_text(card, ".posting-listing-company")
            loc = await safe_inner_text(card, ".posting-listing-location")
            job_url = await safe_get_attribute(card, "a", "href")
            if not title:
                continue
            jobs.append({
                "source": "handshake",
                "source_job_id": None,
                "title": title,
                "company": company,
                "location": loc,
                "is_remote": is_remote_location(loc, title),
                "url": job_url,
                "description": "",
                "experience_level": parse_experience_level(title, ""),
            })
        await browser.close()
        return jobs


async def fetch(session_cookie: str, query: str, location: str) -> list[dict]:
    try:
        return await _scrape(session_cookie, query, location)
    except Exception as exc:
        logger.error("Handshake scraper error: %s", exc)
        return []
```

- [ ] **Step 5: Run all scraper tests**

```bash
docker compose run --rm web pytest tests/test_job_sources.py::TestWellfoundScraper tests/test_job_sources.py::TestDiceScraper tests/test_job_sources.py::TestHandshakeScraper -v
```

Expected: 6 passed

- [ ] **Step 6: Commit**

```bash
git add app/services/sources/wellfound.py app/services/sources/dice.py app/services/sources/handshake.py tests/test_job_sources.py
git commit -m "feat: Wellfound, Dice, Handshake playwright scrapers with tests"
```

---

## Task 10: Job fetcher orchestrator + tests

**Files:**
- Create: `app/services/job_fetcher.py`
- Create: `tests/test_fetch_task.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_fetch_task.py`:

```python
from unittest.mock import patch, MagicMock

import pytest

from app.models.job import Job, JobStatus
from app.services.profile_service import get_or_create_profile, save_section


def _make_profile_with_targets(db):
    get_or_create_profile(db)
    save_section(db, "personal", {"name": "Jay", "email": "jay@test.com",
                                   "phone": "", "linkedin": "", "github": "", "location": ""})
    profile = db.query(__import__("app.models.profile", fromlist=["Profile"]).Profile).first()
    import copy
    data = copy.deepcopy(profile.data)
    data["target_roles"] = ["Software Engineer"]
    data["target_locations"] = ["New York, NY"]
    profile.data = data
    db.flush()
    return profile


def _std_job(*, title="SWE", company="ACME", location="NYC",
             url="https://ex.com/1", source_job_id="J1",
             description="Build things.") -> dict:
    return {
        "source": "adzuna",
        "source_job_id": source_job_id,
        "title": title,
        "company": company,
        "location": location,
        "is_remote": False,
        "url": url,
        "description": description,
        "experience_level": "mid",
    }


class TestFetchAndSaveJobs:
    def test_inserts_new_job(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        jobs = [_std_job()]
        with patch("app.services.job_fetcher._run_all_adapters", return_value=jobs):
            result = fetch_and_save_jobs(db)
        assert result["inserted"] == 1
        assert result["fetched"] == 1
        saved = db.query(Job).first()
        assert saved is not None
        assert saved.status == JobStatus.new

    def test_skips_duplicate_url(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        jobs = [_std_job(url="https://ex.com/1", dedupe_hash_override=None)]
        # Insert first
        with patch("app.services.job_fetcher._run_all_adapters", return_value=jobs):
            r1 = fetch_and_save_jobs(db)
        # Insert again — same URL
        with patch("app.services.job_fetcher._run_all_adapters", return_value=jobs):
            r2 = fetch_and_save_jobs(db)
        assert r1["inserted"] == 1
        assert r2["skipped"] == 1
        assert db.query(Job).count() == 1

    def test_merges_cross_posted_job(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        # Same company/title/location but different URLs
        j1 = _std_job(url="https://adzuna.com/1", source_job_id="AZ1")
        j2 = {**_std_job(url="https://indeed.com/1", source_job_id=None),
              "source": "indeed"}
        with patch("app.services.job_fetcher._run_all_adapters", return_value=[j1]):
            fetch_and_save_jobs(db)
        with patch("app.services.job_fetcher._run_all_adapters", return_value=[j2]):
            r2 = fetch_and_save_jobs(db)
        assert r2["merged"] == 1
        job = db.query(Job).first()
        assert "https://indeed.com/1" in job.source_urls

    def test_no_profile_returns_zeros(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs
        result = fetch_and_save_jobs(db)
        assert result == {"fetched": 0, "inserted": 0, "merged": 0, "skipped": 0}

    def test_empty_target_roles_returns_zeros(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs
        get_or_create_profile(db)
        db.flush()
        result = fetch_and_save_jobs(db)
        assert result == {"fetched": 0, "inserted": 0, "merged": 0, "skipped": 0}

    def test_adapter_error_does_not_crash(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        with patch("app.services.job_fetcher._run_all_adapters",
                   side_effect=RuntimeError("adapter exploded")):
            result = fetch_and_save_jobs(db)
        assert result["fetched"] == 0

    def test_multiple_jobs_counted_correctly(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        jobs = [
            _std_job(title="SWE", company="Alpha", url="https://ex.com/a", source_job_id="A1"),
            _std_job(title="SRE", company="Beta", url="https://ex.com/b", source_job_id="B1"),
            _std_job(title="DevOps", company="Gamma", url="https://ex.com/c", source_job_id="C1"),
        ]
        with patch("app.services.job_fetcher._run_all_adapters", return_value=jobs):
            result = fetch_and_save_jobs(db)
        assert result["inserted"] == 3
        assert result["merged"] == 0
        assert result["skipped"] == 0
```

- [ ] **Step 2: Run tests (expect failure)**

```bash
docker compose run --rm web pytest tests/test_fetch_task.py::TestFetchAndSaveJobs -q
```

Expected: ImportError

- [ ] **Step 3: Create `app/tasks/__init__.py`**

Empty file.

- [ ] **Step 4: Create `app/services/job_fetcher.py`**

```python
import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import settings
from app.models.job import Job, JobStatus
from app.models.profile import Profile
from app.services.deduplication import compute_dedupe_hash, find_existing_job, merge_or_skip

logger = logging.getLogger(__name__)


def _get_slugs(raw: str) -> list[str]:
    return [s.strip() for s in raw.split(",") if s.strip()]


def _run_all_adapters(roles: list[str], locations: list[str], cfg) -> list[dict]:
    """
    Call all enabled Tier 1 (httpx) and Tier 2 (Playwright) adapters.
    Each adapter wraps errors internally and returns []. Never raises.
    """
    all_jobs: list[dict] = []

    # Tier 1: httpx adapters
    if cfg.ADZUNA_APP_ID and cfg.ADZUNA_APP_KEY:
        from app.services.sources.adzuna import fetch as adzuna_fetch
        for role in roles:
            for loc in locations:
                try:
                    all_jobs.extend(adzuna_fetch(
                        app_id=cfg.ADZUNA_APP_ID, app_key=cfg.ADZUNA_APP_KEY,
                        query=role, location=loc,
                    ))
                except Exception as exc:
                    logger.error("Adzuna error for '%s'/'%s': %s", role, loc, exc)

    if cfg.JSEARCH_API_KEY:
        from app.services.sources.jsearch import fetch as jsearch_fetch
        for role in roles:
            for loc in locations:
                try:
                    all_jobs.extend(jsearch_fetch(
                        api_key=cfg.JSEARCH_API_KEY, query=role, location=loc,
                    ))
                except Exception as exc:
                    logger.error("JSearch error for '%s'/'%s': %s", role, loc, exc)

    greenhouse_slugs = _get_slugs(cfg.GREENHOUSE_COMPANY_SLUGS)
    if greenhouse_slugs:
        from app.services.sources.greenhouse import fetch as gh_fetch
        try:
            all_jobs.extend(gh_fetch(company_slugs=greenhouse_slugs))
        except Exception as exc:
            logger.error("Greenhouse error: %s", exc)

    lever_slugs = _get_slugs(cfg.LEVER_COMPANY_SLUGS)
    if lever_slugs:
        from app.services.sources.lever import fetch as lever_fetch
        try:
            all_jobs.extend(lever_fetch(company_slugs=lever_slugs))
        except Exception as exc:
            logger.error("Lever error: %s", exc)

    ashby_slugs = _get_slugs(cfg.ASHBY_COMPANY_SLUGS)
    if ashby_slugs:
        from app.services.sources.ashby import fetch as ashby_fetch
        try:
            all_jobs.extend(ashby_fetch(company_slugs=ashby_slugs))
        except Exception as exc:
            logger.error("Ashby error: %s", exc)

    # Tier 2: Playwright scrapers (async, run in event loop)
    async def _run_playwright() -> list[dict]:
        pw_jobs: list[dict] = []

        if cfg.LINKEDIN_SESSION_COOKIE:
            from app.services.sources.linkedin import fetch as li_fetch
            for role in roles:
                for loc in locations:
                    try:
                        pw_jobs.extend(await li_fetch(
                            session_cookie=cfg.LINKEDIN_SESSION_COOKIE,
                            query=role, location=loc,
                        ))
                    except Exception as exc:
                        logger.error("LinkedIn error: %s", exc)

        for role in roles:
            for loc in locations:
                from app.services.sources.indeed import fetch as indeed_fetch
                try:
                    pw_jobs.extend(await indeed_fetch(query=role, location=loc))
                except Exception as exc:
                    logger.error("Indeed error: %s", exc)

                from app.services.sources.wellfound import fetch as wf_fetch
                try:
                    pw_jobs.extend(await wf_fetch(query=role, location=loc))
                except Exception as exc:
                    logger.error("Wellfound error: %s", exc)

                from app.services.sources.dice import fetch as dice_fetch
                try:
                    pw_jobs.extend(await dice_fetch(query=role, location=loc))
                except Exception as exc:
                    logger.error("Dice error: %s", exc)

        if getattr(cfg, "HANDSHAKE_SESSION_COOKIE", ""):
            from app.services.sources.handshake import fetch as hs_fetch
            for role in roles:
                try:
                    pw_jobs.extend(await hs_fetch(
                        session_cookie=cfg.HANDSHAKE_SESSION_COOKIE,
                        query=role, location="",
                    ))
                except Exception as exc:
                    logger.error("Handshake error: %s", exc)

        return pw_jobs

    try:
        pw_results = asyncio.run(_run_playwright())
        all_jobs.extend(pw_results)
    except Exception as exc:
        logger.error("Playwright scrapers fatal error: %s", exc)

    return all_jobs


def fetch_and_save_jobs(db: Session) -> dict:
    counts = {"fetched": 0, "inserted": 0, "merged": 0, "skipped": 0}

    profile = db.query(Profile).first()
    if not profile:
        logger.warning("job_fetcher: no profile found, skipping.")
        return counts

    roles: list[str] = profile.data.get("target_roles") or []
    locations: list[str] = profile.data.get("target_locations") or []

    if not roles or not locations:
        logger.warning("job_fetcher: target_roles or target_locations empty.")
        return counts

    try:
        raw_jobs = _run_all_adapters(roles, locations, settings)
    except Exception as exc:
        logger.error("job_fetcher: _run_all_adapters failed: %s", exc)
        return counts

    counts["fetched"] = len(raw_jobs)
    now = datetime.now(timezone.utc)

    for job_data in raw_jobs:
        try:
            url = job_data.get("url", "")
            source = job_data.get("source", "")
            source_job_id = job_data.get("source_job_id")
            company = job_data.get("company", "")
            title = job_data.get("title", "")
            location = job_data.get("location", "")
            description = job_data.get("description", "")

            dedupe_hash = compute_dedupe_hash(company, title, location)
            existing = find_existing_job(db, source, url, source_job_id, dedupe_hash)

            if existing is not None:
                # Check if it's a hash-only match (cross-post) vs URL/ID match
                if url in existing.source_urls:
                    counts["skipped"] += 1
                    continue
                if source_job_id and existing.source_job_id == source_job_id and existing.source == source:
                    counts["skipped"] += 1
                    continue
                # Hash match = cross-post: merge URLs
                merge_or_skip(db, existing, url, description, layer=3)
                counts["merged"] += 1
                continue

            new_job = Job(
                source=source,
                source_job_id=source_job_id,
                source_urls=[url],
                title=title,
                company=company,
                location=location,
                is_remote=job_data.get("is_remote", False),
                url=url,
                description=description,
                experience_level=job_data.get("experience_level", "mid"),
                status=JobStatus.new,
                fetched_at=now,
                dedupe_hash=dedupe_hash,
            )
            db.add(new_job)
            db.flush()
            counts["inserted"] += 1

        except Exception as exc:
            logger.error("job_fetcher: error processing job: %s", exc)

    try:
        db.commit()
    except Exception as exc:
        logger.error("job_fetcher: DB commit failed: %s", exc)
        db.rollback()

    return counts
```

- [ ] **Step 5: Run orchestrator tests**

```bash
docker compose run --rm web pytest tests/test_fetch_task.py::TestFetchAndSaveJobs -v
```

Expected: 7 passed

- [ ] **Step 6: Commit**

```bash
git add app/services/job_fetcher.py app/tasks/__init__.py tests/test_fetch_task.py
git commit -m "feat: job fetcher orchestrator with 3-layer dedup integration"
```

---

## Task 11: Celery task + beat schedule + tests

**Files:**
- Create: `app/tasks/fetch.py`
- Modify: `app/celery_app.py`
- Modify: `tests/test_fetch_task.py` (append)

- [ ] **Step 1: Append Celery task tests**

```python
# ---------------------------------------------------------------------------
# Celery task tests
# ---------------------------------------------------------------------------

class TestFetchJobsTask:
    def test_task_is_registered(self):
        from app.celery_app import celery_app
        # Import the task module to register it
        import app.tasks.fetch  # noqa
        assert "app.tasks.fetch.fetch_jobs" in celery_app.tasks

    def test_task_calls_fetch_and_save_jobs(self):
        import app.tasks.fetch  # noqa — ensure task registered
        from app.tasks.fetch import fetch_jobs

        mock_result = {"fetched": 5, "inserted": 3, "merged": 1, "skipped": 1}

        with patch("app.tasks.fetch.fetch_and_save_jobs", return_value=mock_result):
            with patch("app.tasks.fetch.SessionLocal") as mock_session_cls:
                mock_db = MagicMock()
                mock_session_cls.return_value = mock_db
                result = fetch_jobs.apply().result

        assert result == mock_result
        mock_db.close.assert_called_once()

    def test_task_closes_db_on_exception(self):
        import app.tasks.fetch  # noqa
        from app.tasks.fetch import fetch_jobs

        with patch("app.tasks.fetch.fetch_and_save_jobs", side_effect=RuntimeError("DB down")):
            with patch("app.tasks.fetch.SessionLocal") as mock_session_cls:
                mock_db = MagicMock()
                mock_session_cls.return_value = mock_db
                result = fetch_jobs.apply().result

        mock_db.close.assert_called_once()
        assert result == {"fetched": 0, "inserted": 0, "merged": 0, "skipped": 0}

    def test_beat_schedule_configured(self):
        from app.celery_app import celery_app
        schedule = celery_app.conf.beat_schedule
        assert "fetch-jobs-every-5-hours" in schedule
        entry = schedule["fetch-jobs-every-5-hours"]
        assert entry["task"] == "app.tasks.fetch.fetch_jobs"

    def test_beat_schedule_interval_matches_config(self):
        from app.celery_app import celery_app
        from app.config import settings
        schedule = celery_app.conf.beat_schedule
        entry = schedule["fetch-jobs-every-5-hours"]
        expected_seconds = settings.FETCH_INTERVAL_HOURS * 3600
        assert entry["schedule"].seconds == expected_seconds
```

- [ ] **Step 2: Create `app/tasks/fetch.py`**

```python
import logging

from app.celery_app import celery_app
from app.database import SessionLocal
from app.services.job_fetcher import fetch_and_save_jobs

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.fetch.fetch_jobs", bind=True, max_retries=0)
def fetch_jobs(self) -> dict:
    db = SessionLocal()
    try:
        result = fetch_and_save_jobs(db)
        logger.info(
            "fetch_jobs complete — fetched=%d inserted=%d merged=%d skipped=%d",
            result["fetched"], result["inserted"], result["merged"], result["skipped"],
        )
        return result
    except Exception as exc:
        logger.error("fetch_jobs task raised unexpectedly: %s", exc)
        return {"fetched": 0, "inserted": 0, "merged": 0, "skipped": 0}
    finally:
        db.close()
```

- [ ] **Step 3: Update `app/celery_app.py`**

Replace the entire file content:

```python
from celery import Celery
from celery.schedules import schedule as celery_schedule

from app.config import settings

celery_app = Celery(
    "jobapp",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["app.tasks.fetch"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    worker_prefetch_multiplier=1,
)

celery_app.conf.beat_schedule = {
    "fetch-jobs-every-5-hours": {
        "task": "app.tasks.fetch.fetch_jobs",
        "schedule": celery_schedule(settings.FETCH_INTERVAL_HOURS * 3600),
    },
}


@celery_app.task
def ping():
    return "pong"
```

- [ ] **Step 4: Run Celery task tests**

```bash
docker compose run --rm web pytest tests/test_fetch_task.py::TestFetchJobsTask -v
```

Expected: 5 passed

- [ ] **Step 5: Confirm ping regression**

```bash
docker compose run --rm web pytest tests/test_celery.py -v
```

Expected: 2 passed

- [ ] **Step 6: Commit**

```bash
git add app/tasks/fetch.py app/celery_app.py tests/test_fetch_task.py
git commit -m "feat: fetch_jobs Celery task with beat schedule every FETCH_INTERVAL_HOURS"
```

---

## Task 12: Full test suite pass

**Goal:** All tests green. Run and fix any failures.

- [ ] **Step 1: Run full suite**

```bash
docker compose run --rm web pytest tests/ -v --tb=short 2>&1 | tail -40
```

- [ ] **Step 2: Triage common failures**

**`ModuleNotFoundError: No module named 'playwright'`**
Add to Dockerfile after pip install:
```dockerfile
RUN playwright install chromium --with-deps
```
Rebuild: `docker compose build web`

**`asyncio_mode` warnings or errors**
Confirm `pyproject.toml`:
```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

**`IntegrityError` in deduplication tests**
Each test in `TestFindExistingJob` uses a unique `dedupe_hash` (a*32, b*32, c*32, etc.). Verify no collision.

**`AttributeError: type object 'Job' has no attribute 'source_urls'`**
The ARRAY column uses PostgreSQL `any()`. Ensure test DB is PostgreSQL (`TEST_DATABASE_URL` in `.env`). SQLite does not support ARRAY.

**`KeyError: 'app.tasks.fetch.fetch_jobs' not in celery_app.tasks`**
The task is registered on import. Add `import app.tasks.fetch` before the assertion. Already handled in the test.

- [ ] **Step 3: Confirm final count**

```bash
docker compose run --rm web pytest tests/ -q
```

Expected summary (approximate):
```
XX passed in X.XXs
```

Breakdown:
- Plans 01-02 baseline: 42
- `test_deduplication.py`: 7 (parse_experience_level) + 5 (compute_dedupe_hash) + 5 (find_existing_job) + 3 (merge_or_skip) = 20
- `test_job_sources.py`: 4 (Adzuna) + 3 (JSearch) + 3 (Greenhouse) + 2 (Lever) + 2 (Ashby) + 2 (LinkedIn) + 2 (Indeed) + 2 (Wellfound) + 2 (Dice) + 2 (Handshake) = 24
- `test_fetch_task.py`: 7 (orchestrator) + 5 (celery task) = 12

Total: **42 + 20 + 24 + 12 = 98 tests**

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "feat: plan 03 complete — job fetching pipeline, all tests passing"
```
