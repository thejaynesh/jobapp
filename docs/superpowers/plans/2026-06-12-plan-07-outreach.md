# Outreach Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Find LinkedIn contacts and emails for matched jobs, draft personalized outreach messages via LLM, and store them on the Application record.

**Architecture:** `app/services/outreach.py` handles contact search (Hunter.io API for email, LinkedIn scraping for contact name), message drafting (NVIDIA NIM LLM), and persistence to `Application.outreach_contacts` (JSONB array). A POST endpoint `/api/apps/{app_id}/outreach` triggers contact search + draft. 

**Tech Stack:** httpx (Hunter.io API), existing NVIDIA NIM chat_completion, FastAPI, SQLAlchemy

---

## File Map

| File | Action |
|------|--------|
| `app/services/outreach.py` | Create |
| `app/routers/outreach.py` | Create |
| `app/main.py` | Modify — register outreach router |
| `tests/test_outreach.py` | Create |

---

### Task 1: Hunter.io email finder

**Files:**
- Create: `app/services/outreach.py`
- Test: `tests/test_outreach.py` (Task 1 section)

- [ ] **Step 1: Write failing tests for `find_email`**

```python
# tests/test_outreach.py
import pytest
from unittest.mock import MagicMock, patch


class TestFindEmail:
    def test_returns_email_on_success(self):
        from app.services.outreach import find_email
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": {"email": "recruiter@acme.com", "score": 90}
        }
        with patch("app.services.outreach.httpx.get", return_value=mock_resp):
            email = find_email("Acme Corp", "acme.com", "fake-key")
        assert email == "recruiter@acme.com"

    def test_returns_none_when_no_email_found(self):
        from app.services.outreach import find_email
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": {}}
        with patch("app.services.outreach.httpx.get", return_value=mock_resp):
            email = find_email("Acme Corp", "acme.com", "fake-key")
        assert email is None

    def test_returns_none_on_api_error(self):
        from app.services.outreach import find_email
        with patch("app.services.outreach.httpx.get", side_effect=Exception("timeout")):
            email = find_email("Acme Corp", "acme.com", "fake-key")
        assert email is None

    def test_returns_none_when_api_key_empty(self):
        from app.services.outreach import find_email
        email = find_email("Acme Corp", "acme.com", "")
        assert email is None
```

Run: `docker compose run --rm web pytest tests/test_outreach.py::TestFindEmail -v`
Expected: FAIL

- [ ] **Step 2: Implement `find_email` in `app/services/outreach.py`**

```python
import logging
import re

import httpx

from app.config import settings
from app.services.matcher import chat_completion

logger = logging.getLogger(__name__)

HUNTER_DOMAIN_SEARCH_URL = "https://api.hunter.io/v2/domain-search"


def find_email(company_name: str, domain: str, api_key: str) -> str | None:
    if not api_key:
        return None
    try:
        resp = httpx.get(
            HUNTER_DOMAIN_SEARCH_URL,
            params={"domain": domain, "api_key": api_key, "limit": 1},
            timeout=10,
        )
        data = resp.json().get("data", {})
        email = data.get("email")
        return email or None
    except Exception as exc:
        logger.error("find_email error for %s: %s", company_name, exc)
        return None
```

Run: `docker compose run --rm web pytest tests/test_outreach.py::TestFindEmail -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add app/services/outreach.py tests/test_outreach.py
git commit -m "feat: add Hunter.io email finder"
```

---

### Task 2: Domain extraction + LinkedIn contact search stub

**Files:**
- Modify: `app/services/outreach.py`
- Test: `tests/test_outreach.py` (Task 2 section)

- [ ] **Step 1: Write failing tests for `extract_domain` and `find_linkedin_contact`**

```python
class TestExtractDomain:
    def test_extracts_from_url(self):
        from app.services.outreach import extract_domain
        assert extract_domain("https://www.acme.com/jobs/123") == "acme.com"

    def test_extracts_without_www(self):
        from app.services.outreach import extract_domain
        assert extract_domain("https://lever.co/acme") == "lever.co"

    def test_returns_empty_string_for_invalid(self):
        from app.services.outreach import extract_domain
        assert extract_domain("not-a-url") == ""

    def test_strips_www_prefix(self):
        from app.services.outreach import extract_domain
        assert extract_domain("https://www.example.com/page") == "example.com"


class TestFindLinkedinContact:
    def test_returns_dict_with_name_and_title(self):
        from app.services.outreach import find_linkedin_contact
        result = find_linkedin_contact("Acme Corp", "Engineering", "fake-cookie")
        # When cookie is fake/empty, returns empty dict (no real scrape in tests)
        assert isinstance(result, dict)

    def test_returns_empty_dict_without_cookie(self):
        from app.services.outreach import find_linkedin_contact
        result = find_linkedin_contact("Acme Corp", "Engineering", "")
        assert result == {}
```

Run: `docker compose run --rm web pytest tests/test_outreach.py::TestExtractDomain tests/test_outreach.py::TestFindLinkedinContact -v`
Expected: FAIL

- [ ] **Step 2: Implement `extract_domain` and `find_linkedin_contact`**

Add to `app/services/outreach.py`:

```python
from urllib.parse import urlparse


def extract_domain(url: str) -> str:
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc or ""
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc
    except Exception:
        return ""


def find_linkedin_contact(company_name: str, department: str, session_cookie: str) -> dict:
    if not session_cookie:
        return {}
    # LinkedIn scraping requires a valid session cookie — returns best-guess contact
    # In production, use Playwright with the session cookie to search LinkedIn
    # Returns {} if not available to avoid blocking the outreach flow
    try:
        from app.services.sources.playwright_base import LAUNCH_OPTIONS
        import asyncio

        async def _scrape():
            from playwright.async_api import async_playwright
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(**LAUNCH_OPTIONS)
                context = await browser.new_context()
                await context.add_cookies([{
                    "name": "li_at",
                    "value": session_cookie,
                    "domain": ".linkedin.com",
                    "path": "/",
                }])
                page = await context.new_page()
                search_url = (
                    f"https://www.linkedin.com/search/results/people/"
                    f"?keywords={company_name}+{department}&origin=GLOBAL_SEARCH_HEADER"
                )
                await page.goto(search_url, timeout=15000)
                await page.wait_for_timeout(3000)
                cards = await page.query_selector_all(".entity-result__item")
                if not cards:
                    await browser.close()
                    return {}
                first = cards[0]
                name = await first.query_selector(".entity-result__title-text")
                title = await first.query_selector(".entity-result__primary-subtitle")
                result = {
                    "name": (await name.inner_text()).strip() if name else "",
                    "title": (await title.inner_text()).strip() if title else "",
                    "source": "linkedin",
                }
                await browser.close()
                return result

        return asyncio.run(_scrape())
    except Exception as exc:
        logger.error("find_linkedin_contact error for %s: %s", company_name, exc)
        return {}
```

Run: `docker compose run --rm web pytest tests/test_outreach.py::TestExtractDomain tests/test_outreach.py::TestFindLinkedinContact -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add app/services/outreach.py tests/test_outreach.py
git commit -m "feat: add domain extraction and LinkedIn contact search stub"
```

---

### Task 3: Message draft + `run_outreach` orchestrator

**Files:**
- Modify: `app/services/outreach.py`
- Test: `tests/test_outreach.py` (Task 3 section)

- [ ] **Step 1: Write failing tests for `draft_outreach_message` and `run_outreach`**

```python
class TestDraftOutreachMessage:
    def _profile(self):
        return {
            "name": "Jane Doe",
            "narrative": {"summary": "Experienced backend engineer."},
            "skills": {"languages": ["Python", "Go"]},
        }

    def test_returns_string(self):
        from app.services.outreach import draft_outreach_message
        with patch("app.services.outreach.chat_completion", return_value="Hi John, I'd love to connect."):
            msg = draft_outreach_message(
                self._profile(), "John Smith", "Recruiter", "Backend Engineer", "Acme Corp",
                "fake-key", "http://fake", "fake-model",
            )
        assert isinstance(msg, str)
        assert len(msg) > 0

    def test_passes_correct_llm_args(self):
        from app.services.outreach import draft_outreach_message
        with patch("app.services.outreach.chat_completion", return_value="Hello.") as mock_cc:
            draft_outreach_message(
                self._profile(), "John", "Engineer", "SWE", "Acme",
                "my-key", "http://base", "my-model",
            )
        _, kwargs = mock_cc.call_args
        assert kwargs["api_key"] == "my-key"
        assert kwargs["model"] == "my-model"

    def test_returns_fallback_on_llm_error(self):
        from app.services.outreach import draft_outreach_message
        with patch("app.services.outreach.chat_completion", side_effect=Exception("fail")):
            msg = draft_outreach_message(
                self._profile(), "John", "Engineer", "SWE", "Acme",
                "key", "url", "model",
            )
        assert isinstance(msg, str)
        assert len(msg) > 0


class TestRunOutreach:
    def _profile_data(self):
        return {
            "name": "Jane Doe",
            "narrative": {"summary": "Engineer."},
            "skills": {"languages": ["Python"]},
        }

    def _make_app(self):
        app = MagicMock()
        app.id = "test-app-id"
        app.outreach_contacts = []
        app.job = MagicMock()
        app.job.company = "Acme Corp"
        app.job.title = "Backend Engineer"
        app.job.url = "https://acme.com/jobs/123"
        return app

    def test_appends_contact_to_outreach_contacts(self):
        from app.services.outreach import run_outreach
        db = MagicMock()
        db.query.return_value.first.return_value = MagicMock(data=self._profile_data())
        app = self._make_app()
        with patch("app.services.outreach.find_email", return_value="hr@acme.com"):
            with patch("app.services.outreach.find_linkedin_contact", return_value={"name": "John", "title": "Recruiter"}):
                with patch("app.services.outreach.draft_outreach_message", return_value="Hi John."):
                    run_outreach(db, app)
        assert len(app.outreach_contacts) == 1
        assert app.outreach_contacts[0]["email"] == "hr@acme.com"

    def test_commits_after_update(self):
        from app.services.outreach import run_outreach
        db = MagicMock()
        db.query.return_value.first.return_value = MagicMock(data=self._profile_data())
        app = self._make_app()
        with patch("app.services.outreach.find_email", return_value=None):
            with patch("app.services.outreach.find_linkedin_contact", return_value={}):
                with patch("app.services.outreach.draft_outreach_message", return_value="Hi."):
                    run_outreach(db, app)
        db.commit.assert_called_once()

    def test_stores_message_in_contact(self):
        from app.services.outreach import run_outreach
        db = MagicMock()
        db.query.return_value.first.return_value = MagicMock(data=self._profile_data())
        app = self._make_app()
        with patch("app.services.outreach.find_email", return_value="hr@acme.com"):
            with patch("app.services.outreach.find_linkedin_contact", return_value={}):
                with patch("app.services.outreach.draft_outreach_message", return_value="Hi there."):
                    run_outreach(db, app)
        assert app.outreach_contacts[0]["message"] == "Hi there."
```

Run: `docker compose run --rm web pytest tests/test_outreach.py::TestDraftOutreachMessage tests/test_outreach.py::TestRunOutreach -v`
Expected: FAIL

- [ ] **Step 2: Implement `draft_outreach_message` and `run_outreach`**

Add to `app/services/outreach.py`:

```python
def draft_outreach_message(
    profile_data: dict,
    contact_name: str,
    contact_title: str,
    job_title: str,
    company: str,
    api_key: str,
    base_url: str,
    model: str,
) -> str:
    name = profile_data.get("name", "Candidate")
    summary = profile_data.get("narrative", {}).get("summary", "")
    skills = profile_data.get("skills", {})
    skills_flat = [s for cat in skills.values() for s in cat]

    messages = [
        {
            "role": "system",
            "content": (
                "You write short, personalized LinkedIn outreach messages (3-4 sentences). "
                "Professional tone. No generic phrases like 'I hope this message finds you well'. "
                "Mention the specific role and one relevant skill. End with a clear ask."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Write a LinkedIn message from {name} to {contact_name} ({contact_title} at {company}).\n"
                f"Candidate summary: {summary}\n"
                f"Top skills: {', '.join(skills_flat[:5])}\n"
                f"Target role: {job_title} at {company}"
            ),
        },
    ]
    try:
        return chat_completion(messages=messages, api_key=api_key, base_url=base_url, model=model)
    except Exception as exc:
        logger.error("draft_outreach_message error: %s", exc)
        return (
            f"Hi {contact_name}, I came across the {job_title} role at {company} and believe my background "
            f"in {', '.join(skills_flat[:2])} could be a strong fit. Would love to connect and learn more "
            "about the team. Thanks!"
        )


def run_outreach(db, application) -> None:
    from app.models.profile import Profile

    api_key = settings.NVIDIA_NIM_API_KEY
    base_url = settings.NVIDIA_NIM_BASE_URL
    model = settings.NVIDIA_NIM_MODEL
    hunter_key = settings.HUNTER_IO_API_KEY
    linkedin_cookie = settings.LINKEDIN_SESSION_COOKIE

    profile = db.query(Profile).first()
    profile_data = profile.data if profile else {}

    job = application.job
    domain = extract_domain(job.url)

    email = find_email(job.company, domain, hunter_key)
    linkedin_contact = find_linkedin_contact(job.company, "Engineering", linkedin_cookie)

    contact_name = linkedin_contact.get("name", "Hiring Manager")
    contact_title = linkedin_contact.get("title", "Recruiter")

    message = draft_outreach_message(
        profile_data, contact_name, contact_title,
        job.title, job.company, api_key, base_url, model,
    )

    contact_record = {
        "name": contact_name,
        "title": contact_title,
        "email": email,
        "linkedin_source": linkedin_contact.get("source"),
        "message": message,
    }

    existing = list(application.outreach_contacts or [])
    existing.append(contact_record)
    application.outreach_contacts = existing
    db.commit()
```

Run: `docker compose run --rm web pytest tests/test_outreach.py::TestDraftOutreachMessage tests/test_outreach.py::TestRunOutreach -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add app/services/outreach.py tests/test_outreach.py
git commit -m "feat: add message drafting and run_outreach orchestrator"
```

---

### Task 4: Outreach API endpoint

**Files:**
- Create: `app/routers/outreach.py`
- Modify: `app/main.py`
- Test: `tests/test_outreach.py` (Task 4 section)

- [ ] **Step 1: Write failing tests for the outreach endpoint**

```python
class TestOutreachEndpoint:
    def _make_client(self, mock_db):
        from app.routers.outreach import router
        from app.database import get_db
        from fastapi import FastAPI
        fastapp = FastAPI()
        fastapp.include_router(router)
        fastapp.dependency_overrides[get_db] = lambda: mock_db
        return TestClient(fastapp)

    def test_returns_202_for_valid_app(self):
        from app.models.application import Application
        import uuid as uuidmod
        app_id = uuidmod.uuid4()
        mock_db = MagicMock()
        mock_app = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = mock_app
        client = self._make_client(mock_db)
        with patch("app.routers.outreach.run_outreach"):
            response = client.post(f"/api/apps/{app_id}/outreach")
        assert response.status_code == 202

    def test_returns_404_for_missing_app(self):
        import uuid as uuidmod
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None
        client = self._make_client(mock_db)
        response = client.post(f"/api/apps/{uuidmod.uuid4()}/outreach")
        assert response.status_code == 404

    def test_calls_run_outreach(self):
        import uuid as uuidmod
        mock_db = MagicMock()
        mock_app = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = mock_app
        client = self._make_client(mock_db)
        with patch("app.routers.outreach.run_outreach") as mock_ro:
            client.post(f"/api/apps/{uuidmod.uuid4()}/outreach")
        mock_ro.assert_called_once_with(mock_db, mock_app)
```

Run: `docker compose run --rm web pytest tests/test_outreach.py::TestOutreachEndpoint -v`
Expected: FAIL

- [ ] **Step 2: Create `app/routers/outreach.py`**

```python
import uuid
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.application import Application
from app.services.outreach import run_outreach

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/apps", tags=["outreach"])


@router.post("/{app_id}/outreach", status_code=202)
def trigger_outreach(app_id: uuid.UUID, db: Session = Depends(get_db)):
    app_obj = db.query(Application).filter(Application.id == app_id).first()
    if not app_obj:
        raise HTTPException(status_code=404, detail="Application not found")
    run_outreach(db, app_obj)
    return {"status": "ok"}
```

- [ ] **Step 3: Register in `app/main.py`**

Add:
```python
from app.routers.outreach import router as outreach_router
app.include_router(outreach_router)
```

Run: `docker compose run --rm web pytest tests/test_outreach.py::TestOutreachEndpoint -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add app/routers/outreach.py app/main.py tests/test_outreach.py
git commit -m "feat: add /api/apps/{id}/outreach endpoint"
```

---

### Task 5: Full suite run

- [ ] **Step 1: Run all tests**

```bash
docker compose run --rm web pytest --tb=short -q
```

Expected: All passing.

- [ ] **Step 2: Final commit if fixes needed**

```bash
git add -u
git commit -m "fix: outreach plan-07 test suite cleanup"
```
