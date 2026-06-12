# Application Tracker UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build /jobs, /apps, and /settings pages using HTMX + Jinja2 templates. Jobs page shows matched and filtered jobs with override capability. Apps page shows tracked applications with status updates. Settings page manages app-level configuration.

**Architecture:** Three new routers (jobs, apps, settings) + Jinja2 templates. HTMX handles status updates and filtering without full page reloads. All static assets served locally (already in place).

**Tech Stack:** FastAPI, Jinja2, HTMX, TailwindCSS (existing static assets), SQLAlchemy

---

## File Map

| File | Action |
|------|--------|
| `app/routers/jobs.py` | Create |
| `app/routers/apps.py` | Create |
| `app/routers/settings.py` | Create |
| `app/templates/jobs/index.html` | Create |
| `app/templates/jobs/partials/job_card.html` | Create |
| `app/templates/apps/index.html` | Create |
| `app/templates/apps/partials/app_row.html` | Create |
| `app/templates/settings/index.html` | Create |
| `app/main.py` | Modify — register 3 new routers |
| `tests/test_tracker_ui.py` | Create |

---

### Task 1: Jobs router + templates

**Files:**
- Create: `app/routers/jobs.py`
- Create: `app/templates/jobs/index.html`
- Create: `app/templates/jobs/partials/job_card.html`
- Test: `tests/test_tracker_ui.py` (jobs section)

Jobs page shows all non-new jobs (matched, filtered_out, docs_generated). Query params: `status` filter, `q` search. Override: POST `/jobs/{job_id}/override` toggles `filtered_out` ↔ `matched`.

- [ ] **Step 1: Write failing tests for jobs routes**

```python
# tests/test_tracker_ui.py

import uuid
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from fastapi import FastAPI

from app.models.job import JobStatus


def _make_job(status=JobStatus.matched, title="Backend Engineer", company="Acme"):
    job = MagicMock()
    job.id = uuid.uuid4()
    job.title = title
    job.company = company
    job.location = "Remote"
    job.is_remote = True
    job.url = "https://example.com/job"
    job.status = status
    job.llm_score = 85
    job.keyword_score = 0.8
    job.llm_reasoning = "Strong fit."
    job.matched_skills = ["Python", "FastAPI"]
    job.missing_skills = ["Rust"]
    job.source = "adzuna"
    job.fetched_at = None
    return job


class TestJobsRouter:
    def _make_client(self, mock_db):
        from app.routers.jobs import router
        from app.database import get_db
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = lambda: mock_db
        return TestClient(app)

    def test_get_jobs_returns_200(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [_make_job()]
        client = self._make_client(mock_db)
        response = client.get("/jobs")
        assert response.status_code == 200

    def test_get_jobs_html_contains_job_title(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [_make_job()]
        client = self._make_client(mock_db)
        response = client.get("/jobs")
        assert "Backend Engineer" in response.text

    def test_get_jobs_filters_by_status(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = []
        client = self._make_client(mock_db)
        response = client.get("/jobs?status=matched")
        assert response.status_code == 200

    def test_override_matched_to_filtered(self):
        job = _make_job(status=JobStatus.matched)
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = job
        client = self._make_client(mock_db)
        response = client.post(f"/jobs/{job.id}/override")
        assert response.status_code == 200
        assert job.status == JobStatus.filtered_out

    def test_override_filtered_to_matched(self):
        job = _make_job(status=JobStatus.filtered_out)
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = job
        client = self._make_client(mock_db)
        response = client.post(f"/jobs/{job.id}/override")
        assert response.status_code == 200
        assert job.status == JobStatus.matched

    def test_override_returns_404_for_missing_job(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None
        client = self._make_client(mock_db)
        response = client.post(f"/jobs/{uuid.uuid4()}/override")
        assert response.status_code == 404

    def test_get_jobs_no_jobs_shows_empty_state(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = []
        client = self._make_client(mock_db)
        response = client.get("/jobs")
        assert response.status_code == 200
```

Run: `docker compose run --rm web pytest tests/test_tracker_ui.py::TestJobsRouter -v`
Expected: FAIL (module not found)

- [ ] **Step 2: Create `app/routers/jobs.py`**

```python
import uuid
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.job import Job, JobStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["jobs"])
templates = Jinja2Templates(directory="app/templates")

_FILTERABLE_STATUSES = [JobStatus.matched, JobStatus.filtered_out, JobStatus.docs_generated]


@router.get("", response_class=HTMLResponse)
def get_jobs(request: Request, status: str = "", q: str = "", db: Session = Depends(get_db)):
    query = db.query(Job).filter(Job.status.in_(_FILTERABLE_STATUSES))
    if status:
        try:
            query = query.filter(Job.status == JobStatus(status))
        except ValueError:
            pass
    if q:
        query = query.filter(
            (Job.title.ilike(f"%{q}%")) | (Job.company.ilike(f"%{q}%"))
        )
    jobs = query.order_by(Job.llm_score.desc().nullslast()).all()
    return templates.TemplateResponse(
        "jobs/index.html",
        {"request": request, "jobs": jobs, "status_filter": status, "q": q},
    )


@router.post("/{job_id}/override", response_class=HTMLResponse)
def override_job_status(job_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status == JobStatus.matched:
        job.status = JobStatus.filtered_out
    elif job.status == JobStatus.filtered_out:
        job.status = JobStatus.matched
    db.commit()
    return templates.TemplateResponse(
        "jobs/partials/job_card.html",
        {"request": request, "job": job},
    )
```

- [ ] **Step 3: Create `app/templates/jobs/index.html`**

```html
{% extends "base.html" %}
{% block title %}Jobs — JobApp{% endblock %}
{% block content %}
<div class="flex justify-between items-center mb-6">
  <h1 class="text-2xl font-bold text-gray-800">Jobs</h1>
  <span class="text-sm text-gray-500">{{ jobs|length }} shown</span>
</div>

<form class="flex gap-3 mb-6" method="get" action="/jobs">
  <input type="text" name="q" value="{{ q }}" placeholder="Search title or company..."
    class="border border-gray-300 rounded px-3 py-2 text-sm flex-1 focus:outline-none focus:ring-2 focus:ring-blue-500">
  <select name="status" class="border border-gray-300 rounded px-3 py-2 text-sm focus:outline-none">
    <option value="" {% if not status_filter %}selected{% endif %}>All statuses</option>
    <option value="matched" {% if status_filter == "matched" %}selected{% endif %}>Matched</option>
    <option value="filtered_out" {% if status_filter == "filtered_out" %}selected{% endif %}>Filtered out</option>
    <option value="docs_generated" {% if status_filter == "docs_generated" %}selected{% endif %}>Docs generated</option>
  </select>
  <button type="submit" class="bg-blue-600 text-white px-4 py-2 rounded text-sm hover:bg-blue-700">Filter</button>
</form>

<div id="job-list" class="space-y-4">
{% if jobs %}
  {% for job in jobs %}
    {% include "jobs/partials/job_card.html" %}
  {% endfor %}
{% else %}
  <div class="text-center py-16 text-gray-400">
    <p class="text-lg">No jobs found.</p>
    <p class="text-sm mt-1">Jobs are fetched every 5 hours. Check back soon.</p>
  </div>
{% endif %}
</div>
{% endblock %}
```

- [ ] **Step 4: Create `app/templates/jobs/partials/job_card.html`**

```html
<div id="job-{{ job.id }}" class="bg-white border border-gray-200 rounded-lg p-5 shadow-sm">
  <div class="flex justify-between items-start">
    <div>
      <a href="{{ job.url }}" target="_blank" class="text-lg font-semibold text-blue-600 hover:underline">
        {{ job.title }}
      </a>
      <p class="text-gray-600 text-sm mt-0.5">{{ job.company }} &bull; {{ job.location or "Unknown" }}
        {% if job.is_remote %}<span class="ml-2 bg-green-100 text-green-700 text-xs px-2 py-0.5 rounded">Remote</span>{% endif %}
      </p>
    </div>
    <div class="text-right">
      {% if job.llm_score is not none %}
        <span class="text-2xl font-bold {% if job.llm_score >= 75 %}text-green-600{% elif job.llm_score >= 50 %}text-yellow-600{% else %}text-red-500{% endif %}">
          {{ job.llm_score }}
        </span>
        <p class="text-xs text-gray-400">LLM score</p>
      {% endif %}
    </div>
  </div>

  {% if job.llm_reasoning %}
  <p class="text-sm text-gray-500 mt-2 italic">{{ job.llm_reasoning }}</p>
  {% endif %}

  <div class="flex flex-wrap gap-1 mt-3">
    {% for skill in (job.matched_skills or []) %}
      <span class="bg-blue-50 text-blue-700 text-xs px-2 py-0.5 rounded">{{ skill }}</span>
    {% endfor %}
    {% for skill in (job.missing_skills or []) %}
      <span class="bg-red-50 text-red-600 text-xs px-2 py-0.5 rounded line-through">{{ skill }}</span>
    {% endfor %}
  </div>

  <div class="flex items-center justify-between mt-4">
    <span class="text-xs px-2 py-1 rounded font-medium
      {% if job.status.value == 'matched' %}bg-green-100 text-green-700
      {% elif job.status.value == 'filtered_out' %}bg-gray-100 text-gray-500
      {% elif job.status.value == 'docs_generated' %}bg-purple-100 text-purple-700
      {% else %}bg-gray-100 text-gray-500{% endif %}">
      {{ job.status.value | replace("_", " ") | title }}
    </span>
    <div class="flex gap-2">
      {% if job.status.value in ['matched', 'filtered_out'] %}
      <button
        hx-post="/jobs/{{ job.id }}/override"
        hx-target="#job-{{ job.id }}"
        hx-swap="outerHTML"
        class="text-xs text-gray-500 hover:text-blue-600 underline">
        {% if job.status.value == 'matched' %}Mark filtered{% else %}Override to matched{% endif %}
      </button>
      {% endif %}
      {% if job.status.value == 'matched' %}
      <button
        hx-post="/api/jobs/{{ job.id }}/generate-docs"
        hx-swap="none"
        class="text-xs bg-blue-600 text-white px-3 py-1 rounded hover:bg-blue-700">
        Generate docs
      </button>
      {% endif %}
    </div>
  </div>
</div>
```

Run: `docker compose run --rm web pytest tests/test_tracker_ui.py::TestJobsRouter -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/routers/jobs.py app/templates/jobs/ tests/test_tracker_ui.py
git commit -m "feat: add /jobs page with status filter and override capability"
```

---

### Task 2: Applications router + templates

**Files:**
- Create: `app/routers/apps.py`
- Create: `app/templates/apps/index.html`
- Create: `app/templates/apps/partials/app_row.html`
- Test: `tests/test_tracker_ui.py` (apps section)

Apps page shows all applications with their linked job + status. HTMX status update via POST `/apps/{app_id}/status`.

- [ ] **Step 1: Write failing tests for apps routes**

```python
class TestAppsRouter:
    def _make_app_obj(self, status="not_applied"):
        from app.models.application import ApplicationStatus
        app_obj = MagicMock()
        app_obj.id = uuid.uuid4()
        app_obj.status = ApplicationStatus(status)
        app_obj.notes = ""
        app_obj.applied_at = None
        app_obj.created_at = None
        app_obj.job = MagicMock()
        app_obj.job.id = uuid.uuid4()
        app_obj.job.title = "Backend Engineer"
        app_obj.job.company = "Acme"
        app_obj.job.url = "https://example.com"
        app_obj.documents = []
        return app_obj

    def _make_client(self, mock_db):
        from app.routers.apps import router
        from app.database import get_db
        from fastapi import FastAPI
        fastapp = FastAPI()
        fastapp.include_router(router)
        fastapp.dependency_overrides[get_db] = lambda: mock_db
        return TestClient(fastapp)

    def test_get_apps_returns_200(self):
        mock_db = MagicMock()
        mock_db.query.return_value.order_by.return_value.all.return_value = [self._make_app_obj()]
        client = self._make_client(mock_db)
        response = client.get("/apps")
        assert response.status_code == 200

    def test_get_apps_shows_job_title(self):
        mock_db = MagicMock()
        mock_db.query.return_value.order_by.return_value.all.return_value = [self._make_app_obj()]
        client = self._make_client(mock_db)
        response = client.get("/apps")
        assert "Backend Engineer" in response.text

    def test_update_status_returns_200(self):
        app_obj = self._make_app_obj()
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = app_obj
        client = self._make_client(mock_db)
        response = client.post(f"/apps/{app_obj.id}/status", data={"status": "applied"})
        assert response.status_code == 200

    def test_update_status_changes_status(self):
        from app.models.application import ApplicationStatus
        app_obj = self._make_app_obj(status="not_applied")
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = app_obj
        client = self._make_client(mock_db)
        client.post(f"/apps/{app_obj.id}/status", data={"status": "applied"})
        assert app_obj.status == ApplicationStatus.applied

    def test_update_status_returns_404_for_missing(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None
        client = self._make_client(mock_db)
        response = client.post(f"/apps/{uuid.uuid4()}/status", data={"status": "applied"})
        assert response.status_code == 404

    def test_empty_apps_shows_empty_state(self):
        mock_db = MagicMock()
        mock_db.query.return_value.order_by.return_value.all.return_value = []
        client = self._make_client(mock_db)
        response = client.get("/apps")
        assert response.status_code == 200
```

Run: `docker compose run --rm web pytest tests/test_tracker_ui.py::TestAppsRouter -v`
Expected: FAIL

- [ ] **Step 2: Create `app/routers/apps.py`**

```python
import uuid
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.application import Application, ApplicationStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/apps", tags=["apps"])
templates = Jinja2Templates(directory="app/templates")


@router.get("", response_class=HTMLResponse)
def get_apps(request: Request, db: Session = Depends(get_db)):
    apps = db.query(Application).order_by(Application.created_at.desc()).all()
    return templates.TemplateResponse(
        "apps/index.html",
        {"request": request, "apps": apps},
    )


@router.post("/{app_id}/status", response_class=HTMLResponse)
def update_app_status(
    app_id: uuid.UUID,
    request: Request,
    status: str = Form(...),
    db: Session = Depends(get_db),
):
    app_obj = db.query(Application).filter(Application.id == app_id).first()
    if not app_obj:
        raise HTTPException(status_code=404, detail="Application not found")
    try:
        app_obj.status = ApplicationStatus(status)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid status: {status}")
    db.commit()
    return templates.TemplateResponse(
        "apps/partials/app_row.html",
        {"request": request, "app": app_obj},
    )
```

- [ ] **Step 3: Create `app/templates/apps/index.html`**

```html
{% extends "base.html" %}
{% block title %}Applications — JobApp{% endblock %}
{% block content %}
<div class="flex justify-between items-center mb-6">
  <h1 class="text-2xl font-bold text-gray-800">Applications</h1>
  <span class="text-sm text-gray-500">{{ apps|length }} total</span>
</div>

{% if apps %}
<div class="bg-white rounded-lg border border-gray-200 overflow-hidden shadow-sm">
  <table class="w-full text-sm">
    <thead class="bg-gray-50 border-b border-gray-200">
      <tr>
        <th class="px-4 py-3 text-left text-gray-600 font-medium">Job</th>
        <th class="px-4 py-3 text-left text-gray-600 font-medium">Company</th>
        <th class="px-4 py-3 text-left text-gray-600 font-medium">Status</th>
        <th class="px-4 py-3 text-left text-gray-600 font-medium">Docs</th>
        <th class="px-4 py-3 text-left text-gray-600 font-medium">Applied</th>
      </tr>
    </thead>
    <tbody class="divide-y divide-gray-100" id="apps-table">
      {% for app in apps %}
        {% include "apps/partials/app_row.html" %}
      {% endfor %}
    </tbody>
  </table>
</div>
{% else %}
<div class="text-center py-16 text-gray-400">
  <p class="text-lg">No applications yet.</p>
  <p class="text-sm mt-1">Applications appear here once jobs are matched and docs are generated.</p>
</div>
{% endif %}
{% endblock %}
```

- [ ] **Step 4: Create `app/templates/apps/partials/app_row.html`**

```html
<tr id="app-{{ app.id }}">
  <td class="px-4 py-3">
    <a href="{{ app.job.url }}" target="_blank" class="text-blue-600 hover:underline font-medium">
      {{ app.job.title }}
    </a>
  </td>
  <td class="px-4 py-3 text-gray-600">{{ app.job.company }}</td>
  <td class="px-4 py-3">
    <select
      hx-post="/apps/{{ app.id }}/status"
      hx-target="#app-{{ app.id }}"
      hx-swap="outerHTML"
      hx-include="[name='status']"
      name="status"
      class="border border-gray-300 rounded px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500">
      {% for s in ['not_applied', 'applied', 'interviewing', 'offered', 'rejected', 'withdrawn'] %}
        <option value="{{ s }}" {% if app.status.value == s %}selected{% endif %}>
          {{ s | replace("_", " ") | title }}
        </option>
      {% endfor %}
    </select>
  </td>
  <td class="px-4 py-3">
    {% if app.documents %}
      <span class="text-xs text-green-600">{{ app.documents | length }} doc(s)</span>
    {% else %}
      <span class="text-xs text-gray-400">—</span>
    {% endif %}
  </td>
  <td class="px-4 py-3 text-gray-500 text-xs">
    {{ app.applied_at.strftime('%Y-%m-%d') if app.applied_at else '—' }}
  </td>
</tr>
```

Run: `docker compose run --rm web pytest tests/test_tracker_ui.py::TestAppsRouter -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/routers/apps.py app/templates/apps/ tests/test_tracker_ui.py
git commit -m "feat: add /apps tracker page with HTMX status updates"
```

---

### Task 3: Settings router + templates

**Files:**
- Create: `app/routers/settings.py`
- Create: `app/templates/settings/index.html`
- Test: `tests/test_tracker_ui.py` (settings section)

Settings page shows editable config: MIN_MATCH_SCORE, FETCH_INTERVAL_HOURS, MIN_KEYWORD_SKILLS. Saved to `.env` file (or an in-DB settings table if already present). Since there's no settings model, save to `Profile.data["settings"]`.

- [ ] **Step 1: Write failing tests**

```python
class TestSettingsRouter:
    def _make_client(self, mock_db):
        from app.routers.settings import router
        from app.database import get_db
        from fastapi import FastAPI
        fastapp = FastAPI()
        fastapp.include_router(router)
        fastapp.dependency_overrides[get_db] = lambda: mock_db
        return TestClient(fastapp)

    def _mock_profile(self):
        profile = MagicMock()
        profile.data = {
            "settings": {
                "min_match_score": 70,
                "fetch_interval_hours": 5,
                "min_keyword_skills": 2,
            }
        }
        return profile

    def test_get_settings_returns_200(self):
        mock_db = MagicMock()
        mock_db.query.return_value.first.return_value = self._mock_profile()
        client = self._make_client(mock_db)
        response = client.get("/settings")
        assert response.status_code == 200

    def test_get_settings_shows_current_values(self):
        mock_db = MagicMock()
        mock_db.query.return_value.first.return_value = self._mock_profile()
        client = self._make_client(mock_db)
        response = client.get("/settings")
        assert "70" in response.text

    def test_post_settings_saves_values(self):
        mock_db = MagicMock()
        profile = self._mock_profile()
        mock_db.query.return_value.first.return_value = profile
        client = self._make_client(mock_db)
        response = client.post("/settings", data={
            "min_match_score": "80",
            "fetch_interval_hours": "3",
            "min_keyword_skills": "3",
        })
        assert response.status_code == 200
        assert profile.data["settings"]["min_match_score"] == 80

    def test_post_settings_returns_200(self):
        mock_db = MagicMock()
        profile = self._mock_profile()
        mock_db.query.return_value.first.return_value = profile
        client = self._make_client(mock_db)
        response = client.post("/settings", data={
            "min_match_score": "75",
            "fetch_interval_hours": "5",
            "min_keyword_skills": "2",
        })
        assert response.status_code == 200
```

Run: `docker compose run --rm web pytest tests/test_tracker_ui.py::TestSettingsRouter -v`
Expected: FAIL

- [ ] **Step 2: Create `app/routers/settings.py`**

```python
import logging
import copy

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.profile import Profile
from app.services.profile_service import get_or_create_profile

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])
templates = Jinja2Templates(directory="app/templates")

_DEFAULTS = {
    "min_match_score": 70,
    "fetch_interval_hours": 5,
    "min_keyword_skills": 2,
}


@router.get("", response_class=HTMLResponse)
def get_settings(request: Request, db: Session = Depends(get_db)):
    profile = get_or_create_profile(db)
    db.commit()
    current = {**_DEFAULTS, **profile.data.get("settings", {})}
    return templates.TemplateResponse(
        "settings/index.html",
        {"request": request, "settings": current, "saved": False},
    )


@router.post("", response_class=HTMLResponse)
def save_settings(
    request: Request,
    min_match_score: int = Form(70),
    fetch_interval_hours: int = Form(5),
    min_keyword_skills: int = Form(2),
    db: Session = Depends(get_db),
):
    profile = get_or_create_profile(db)
    new_data = copy.deepcopy(profile.data)
    new_data["settings"] = {
        "min_match_score": min_match_score,
        "fetch_interval_hours": fetch_interval_hours,
        "min_keyword_skills": min_keyword_skills,
    }
    profile.data = new_data
    db.commit()
    return templates.TemplateResponse(
        "settings/index.html",
        {"request": request, "settings": new_data["settings"], "saved": True},
    )
```

- [ ] **Step 3: Create `app/templates/settings/index.html`**

```html
{% extends "base.html" %}
{% block title %}Settings — JobApp{% endblock %}
{% block content %}
<h1 class="text-2xl font-bold text-gray-800 mb-6">Settings</h1>

{% if saved %}
<div class="bg-green-50 border border-green-200 text-green-700 rounded px-4 py-3 mb-5 text-sm">
  Settings saved.
</div>
{% endif %}

<form method="post" action="/settings" class="bg-white border border-gray-200 rounded-lg p-6 shadow-sm max-w-lg space-y-5">
  <div>
    <label class="block text-sm font-medium text-gray-700 mb-1">Minimum match score (0–100)</label>
    <input type="number" name="min_match_score" value="{{ settings.min_match_score }}" min="0" max="100"
      class="w-full border border-gray-300 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500">
    <p class="text-xs text-gray-400 mt-1">Jobs below this LLM score are filtered out.</p>
  </div>
  <div>
    <label class="block text-sm font-medium text-gray-700 mb-1">Fetch interval (hours)</label>
    <input type="number" name="fetch_interval_hours" value="{{ settings.fetch_interval_hours }}" min="1" max="24"
      class="w-full border border-gray-300 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500">
    <p class="text-xs text-gray-400 mt-1">How often to fetch new jobs from all sources.</p>
  </div>
  <div>
    <label class="block text-sm font-medium text-gray-700 mb-1">Min keyword skills match</label>
    <input type="number" name="min_keyword_skills" value="{{ settings.min_keyword_skills }}" min="0" max="20"
      class="w-full border border-gray-300 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500">
    <p class="text-xs text-gray-400 mt-1">Minimum skills matched in job description to pass keyword filter.</p>
  </div>
  <button type="submit" class="bg-blue-600 text-white px-6 py-2 rounded text-sm hover:bg-blue-700">Save settings</button>
</form>
{% endblock %}
```

Run: `docker compose run --rm web pytest tests/test_tracker_ui.py::TestSettingsRouter -v`
Expected: PASS

- [ ] **Step 4: Register all three new routers in `app/main.py`**

```python
from app.routers.jobs import router as jobs_router
from app.routers.apps import router as apps_router
from app.routers.settings import router as settings_router

app.include_router(jobs_router)
app.include_router(apps_router)
app.include_router(settings_router)
```

- [ ] **Step 5: Commit**

```bash
git add app/routers/settings.py app/templates/settings/ tests/test_tracker_ui.py
git commit -m "feat: add /settings page with profile-stored config"
```

---

### Task 4: Full suite run

- [ ] **Step 1: Run all tests**

```bash
docker compose run --rm web pytest --tb=short -q
```

Expected: All passing.

- [ ] **Step 2: Register all routers and verify main.py**

Check `app/main.py` includes all routers.

- [ ] **Step 3: Final commit if fixes needed**

```bash
git add -u
git commit -m "fix: tracker UI test suite cleanup"
```
