# Profile & Narrative Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the profile editor web UI — a tabbed Jinja2+HTMX interface for managing personal info, experience, projects, skills, education, LaTeX templates, and a narrative questionnaire that generates a personalized voice summary via NVIDIA NIM.

**Architecture:** FastAPI router at `/profile` with HTMX-powered partials for inline saves and list mutations (add/delete). Profile stored as a single JSONB blob in the `profiles` table. LLM calls (question generation, summary synthesis) go through a thin `app/llm/client.py` wrapper over NVIDIA NIM's OpenAI-compatible API. All list items (experience, projects, education) carry a UUID generated at insert time for stable identification.

**Tech Stack:** FastAPI, Jinja2, HTMX 2.x (CDN), Tailwind CSS (Play CDN), openai SDK 1.x, pytest, httpx (for test client)

---

## File Map

```
app/
├── routers/
│   ├── __init__.py
│   └── profile.py           # all /profile routes
├── services/
│   ├── __init__.py
│   └── profile_service.py   # get_or_create, save_section, list mutations
├── llm/
│   ├── __init__.py
│   └── client.py            # NVIDIA NIM wrapper (chat completion)
├── templates/
│   ├── base.html             # layout: nav + HTMX + Tailwind CDN scripts
│   └── profile/
│       ├── index.html        # tab shell — loads active tab partial via HTMX
│       └── partials/
│           ├── personal.html
│           ├── experience.html       # full list + add button
│           ├── experience_item.html  # single editable card (inline form)
│           ├── projects.html
│           ├── project_item.html
│           ├── skills.html
│           ├── education.html
│           ├── education_item.html
│           ├── templates_tab.html    # LaTeX + cover letter textareas
│           ├── narrative.html        # question list + summary
│           └── narrative_answer.html # single question+answer row
tests/
├── test_profile_service.py
├── test_profile_routes.py
└── test_llm_client.py
```

**Modify:**
- `pyproject.toml` — add `openai==1.58.1`
- `app/main.py` — register profile router, add StaticFiles mount

---

## Default Profile Structure

Every new profile row is seeded with this structure (used in `get_or_create`):

```python
DEFAULT_PROFILE = {
    "personal": {
        "name": "", "email": "", "phone": "",
        "linkedin": "", "github": "", "location": ""
    },
    "experience": [],   # [{id, company, role, start_date, end_date, bullets, tech}]
    "projects": [],     # [{id, name, description, tech, bullets, url}]
    "skills": {
        "languages": [], "frameworks": [], "tools": [], "clouds": []
    },
    "education": [],    # [{id, school, degree, start_date, end_date, gpa}]
    "latex_template": "",
    "cover_letter_template": "",
    "target_roles": [],
    "target_locations": [],
    "excluded_companies": [],
    "min_match_score": 70,
    "narrative": {
        "answers": [],   # [{question, answer}] — populated after generate-questions
        "summary": ""
    }
}
```

---

## Task 1: Add openai dependency

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add openai to dependencies**

Edit `pyproject.toml` — add `"openai==1.58.1"` to the `dependencies` list:

```toml
[project]
name = "jobapp"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi==0.115.5",
    "uvicorn[standard]==0.32.1",
    "sqlalchemy==2.0.36",
    "alembic==1.14.0",
    "psycopg2-binary==2.9.10",
    "celery[redis]==5.4.0",
    "redis==5.2.1",
    "pydantic-settings==2.6.1",
    "python-dotenv==1.0.1",
    "jinja2==3.1.4",
    "httpx==0.27.2",
    "openai==1.58.1",
]
```

- [ ] **Step 2: Rebuild Docker image**

```bash
docker compose build web worker
```

Expected: build completes, `openai` visible in output.

- [ ] **Step 3: Verify openai importable**

```bash
docker compose run --rm web python -c "import openai; print(openai.__version__)"
```

Expected: `1.58.1`

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "chore: add openai sdk dependency"
```

---

## Task 2: LLM Client

**Files:**
- Create: `app/llm/__init__.py`
- Create: `app/llm/client.py`
- Create: `tests/test_llm_client.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_llm_client.py
from unittest.mock import MagicMock, patch
from app.llm.client import chat_completion


def test_chat_completion_calls_nim(monkeypatch):
    mock_response = MagicMock()
    mock_response.choices[0].message.content = "test response"

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_response

    with patch("app.llm.client.OpenAI", return_value=mock_client):
        result = chat_completion(
            messages=[{"role": "user", "content": "hello"}],
            api_key="testkey",
            base_url="https://api.nvidia.com/v1",
            model="meta/llama-3.1-70b-instruct",
        )

    assert result == "test response"
    mock_client.chat.completions.create.assert_called_once()


def test_chat_completion_passes_model(monkeypatch):
    mock_response = MagicMock()
    mock_response.choices[0].message.content = "ok"

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_response

    with patch("app.llm.client.OpenAI", return_value=mock_client):
        chat_completion(
            messages=[{"role": "user", "content": "hi"}],
            api_key="k",
            base_url="https://api.nvidia.com/v1",
            model="custom/model",
        )

    call_kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert call_kwargs["model"] == "custom/model"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker compose run --rm web pytest tests/test_llm_client.py -v
```
Expected: `ImportError: cannot import name 'chat_completion'`

- [ ] **Step 3: Create `app/llm/__init__.py`**

Empty file:
```python
```

- [ ] **Step 4: Write `app/llm/client.py`**

```python
from openai import OpenAI


def chat_completion(
    messages: list[dict],
    api_key: str,
    base_url: str,
    model: str,
    temperature: float = 0.7,
    max_tokens: int = 2048,
) -> str:
    client = OpenAI(api_key=api_key, base_url=base_url)
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
docker compose run --rm web pytest tests/test_llm_client.py -v
```
Expected: `2 passed`

- [ ] **Step 6: Commit**

```bash
git add app/llm/ tests/test_llm_client.py
git commit -m "feat: LLM client wrapper for NVIDIA NIM"
```

---

## Task 3: Profile Service

**Files:**
- Create: `app/services/__init__.py`
- Create: `app/services/profile_service.py`
- Create: `tests/test_profile_service.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_profile_service.py
import pytest
from app.services.profile_service import (
    get_or_create_profile,
    save_section,
    add_list_item,
    remove_list_item,
    DEFAULT_PROFILE,
)


def test_get_or_create_creates_profile(db):
    profile = get_or_create_profile(db)
    assert profile.id is not None
    assert profile.data["personal"]["name"] == ""
    assert profile.data["experience"] == []
    assert profile.data["narrative"]["answers"] == []


def test_get_or_create_returns_existing(db):
    p1 = get_or_create_profile(db)
    db.flush()
    p2 = get_or_create_profile(db)
    assert p1.id == p2.id


def test_save_section_updates_personal(db):
    profile = get_or_create_profile(db)
    db.flush()

    updated = save_section(db, "personal", {"name": "Jay", "email": "jay@example.com"})
    assert updated.data["personal"]["name"] == "Jay"
    assert updated.data["personal"]["email"] == "jay@example.com"
    assert updated.data["experience"] == []  # other sections untouched


def test_save_section_updates_skills(db):
    profile = get_or_create_profile(db)
    db.flush()

    updated = save_section(db, "skills", {
        "languages": ["Python", "Go"],
        "frameworks": ["FastAPI"],
        "tools": [],
        "clouds": ["AWS"],
    })
    assert updated.data["skills"]["languages"] == ["Python", "Go"]


def test_add_list_item_experience(db):
    profile = get_or_create_profile(db)
    db.flush()

    item = {
        "company": "Stripe",
        "role": "SWE",
        "start_date": "2023-01",
        "end_date": "Present",
        "bullets": ["Built payment APIs"],
        "tech": ["Python", "Go"],
    }
    updated = add_list_item(db, "experience", item)
    assert len(updated.data["experience"]) == 1
    assert updated.data["experience"][0]["company"] == "Stripe"
    assert "id" in updated.data["experience"][0]  # UUID assigned


def test_remove_list_item(db):
    profile = get_or_create_profile(db)
    db.flush()

    updated = add_list_item(db, "experience", {"company": "A", "role": "SWE"})
    item_id = updated.data["experience"][0]["id"]

    updated = remove_list_item(db, "experience", item_id)
    assert updated.data["experience"] == []


def test_remove_nonexistent_item_is_noop(db):
    profile = get_or_create_profile(db)
    db.flush()

    updated = remove_list_item(db, "experience", "nonexistent-id")
    assert updated.data["experience"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker compose run --rm web pytest tests/test_profile_service.py -v
```
Expected: `ImportError`

- [ ] **Step 3: Create `app/services/__init__.py`**

Empty file.

- [ ] **Step 4: Write `app/services/profile_service.py`**

```python
import copy
import uuid

from sqlalchemy.orm import Session

from app.models.profile import Profile

DEFAULT_PROFILE: dict = {
    "personal": {
        "name": "", "email": "", "phone": "",
        "linkedin": "", "github": "", "location": ""
    },
    "experience": [],
    "projects": [],
    "skills": {
        "languages": [], "frameworks": [], "tools": [], "clouds": []
    },
    "education": [],
    "latex_template": "",
    "cover_letter_template": "",
    "target_roles": [],
    "target_locations": [],
    "excluded_companies": [],
    "min_match_score": 70,
    "narrative": {
        "answers": [],
        "summary": ""
    }
}


def get_or_create_profile(db: Session) -> Profile:
    profile = db.query(Profile).first()
    if not profile:
        profile = Profile(data=copy.deepcopy(DEFAULT_PROFILE))
        db.add(profile)
        db.flush()
    return profile


def save_section(db: Session, section: str, data: dict | list | str | int) -> Profile:
    profile = get_or_create_profile(db)
    updated = copy.deepcopy(profile.data)
    updated[section] = data
    profile.data = updated
    db.flush()
    return profile


def add_list_item(db: Session, section: str, item: dict) -> Profile:
    profile = get_or_create_profile(db)
    updated = copy.deepcopy(profile.data)
    item_with_id = {"id": str(uuid.uuid4()), **item}
    updated[section].append(item_with_id)
    profile.data = updated
    db.flush()
    return profile


def remove_list_item(db: Session, section: str, item_id: str) -> Profile:
    profile = get_or_create_profile(db)
    updated = copy.deepcopy(profile.data)
    updated[section] = [i for i in updated[section] if i.get("id") != item_id]
    profile.data = updated
    db.flush()
    return profile


def update_list_item(db: Session, section: str, item_id: str, data: dict) -> Profile:
    profile = get_or_create_profile(db)
    updated = copy.deepcopy(profile.data)
    for i, item in enumerate(updated[section]):
        if item.get("id") == item_id:
            updated[section][i] = {"id": item_id, **data}
            break
    profile.data = updated
    db.flush()
    return profile
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
docker compose run --rm web pytest tests/test_profile_service.py -v
```
Expected: `7 passed`

- [ ] **Step 6: Commit**

```bash
git add app/services/ tests/test_profile_service.py
git commit -m "feat: profile service — get_or_create, save_section, list mutations"
```

---

## Task 4: Base Template + Static Assets

**Files:**
- Create: `app/templates/base.html`
- Create: `app/static/css/main.css`
- Modify: `app/main.py` — mount StaticFiles, register profile router placeholder

- [ ] **Step 1: Create static directory and download vendor assets**

Download HTMX and Tailwind CSS locally — no CDN scripts, no SRI concerns:

```bash
mkdir -p app/static/css app/static/js

# HTMX 2.0.3 (minified)
curl -L https://unpkg.com/htmx.org@2.0.3/dist/htmx.min.js -o app/static/js/htmx.min.js

# Tailwind CSS v3 standalone CLI — build a one-time static CSS
# Download the Tailwind standalone CLI binary into the container at build time (see Dockerfile note below)
# For now, download a pre-built Tailwind CSS base from the CDN into local static:
curl -L https://cdn.tailwindcss.com/3.4.17/tailwind.min.css -o app/static/css/tailwind.min.css
```

Verify files downloaded:
```bash
ls -lh app/static/js/htmx.min.js app/static/css/tailwind.min.css
```
Expected: both files present, non-zero size.

- [ ] **Step 2: Write `app/static/css/main.css`**

```css
/* Overrides on top of Tailwind */
.tab-active {
    border-bottom: 2px solid #3b82f6;
    color: #3b82f6;
    font-weight: 600;
}

.htmx-indicator {
    display: none;
}

.htmx-request .htmx-indicator {
    display: inline;
}

.success-flash {
    animation: fadeout 2s forwards;
    animation-delay: 1s;
}

@keyframes fadeout {
    to { opacity: 0; }
}

textarea.latex-editor {
    font-family: 'Courier New', monospace;
    font-size: 0.85rem;
}
```

- [ ] **Step 3: Write `app/templates/base.html`**

Serve HTMX and Tailwind from local static files — no external scripts:

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{% block title %}JobApp{% endblock %}</title>
    <link rel="stylesheet" href="/static/css/tailwind.min.css">
    <link rel="stylesheet" href="/static/css/main.css">
    <script src="/static/js/htmx.min.js" defer></script>
</head>
<body class="bg-gray-50 min-h-screen">
    <nav class="bg-white border-b border-gray-200 px-6 py-3 flex gap-6 items-center">
        <span class="font-bold text-blue-600 text-lg">JobApp</span>
        <a href="/profile" class="text-gray-600 hover:text-blue-600 text-sm">Profile</a>
        <a href="/jobs" class="text-gray-600 hover:text-blue-600 text-sm">Jobs</a>
        <a href="/apps" class="text-gray-600 hover:text-blue-600 text-sm">Applications</a>
        <a href="/settings" class="text-gray-600 hover:text-blue-600 text-sm">Settings</a>
    </nav>
    <main class="max-w-5xl mx-auto px-6 py-8">
        {% block content %}{% endblock %}
    </main>
</body>
</html>
```

- [ ] **Step 4: Modify `app/main.py`** — add StaticFiles mount:

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database import get_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="JobApp", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/health")
def health_check(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception:
        db_status = "error"
    return {"status": "ok", "db": db_status}


@app.get("/")
def root():
    return RedirectResponse(url="/apps")
```

- [ ] **Step 5: Verify health tests still pass**

```bash
docker compose run --rm web pytest tests/test_health.py -v
```
Expected: `2 passed`

- [ ] **Step 6: Commit**

```bash
git add app/static/ app/templates/base.html app/main.py
git commit -m "feat: base template with local Tailwind+HTMX, static files mount"
```

---

## Task 5: Profile Router + Tab Shell

**Files:**
- Create: `app/routers/__init__.py`
- Create: `app/routers/profile.py`
- Create: `app/templates/profile/index.html`
- Create: `tests/test_profile_routes.py` (initial)
- Modify: `app/main.py` — register router

- [ ] **Step 1: Write failing tests**

```python
# tests/test_profile_routes.py
def test_get_profile_returns_200(client):
    response = client.get("/profile")
    assert response.status_code == 200
    assert b"Profile" in response.content


def test_get_profile_tab_personal(client):
    response = client.get("/profile?tab=personal")
    assert response.status_code == 200


def test_get_profile_tab_narrative(client):
    response = client.get("/profile?tab=narrative")
    assert response.status_code == 200
```

- [ ] **Step 2: Run to verify they fail**

```bash
docker compose run --rm web pytest tests/test_profile_routes.py -v
```
Expected: `404` or `ImportError`

- [ ] **Step 3: Create `app/routers/__init__.py`**

Empty file.

- [ ] **Step 4: Write `app/routers/profile.py`** (skeleton — routes added per tab in later tasks)

```python
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.profile_service import get_or_create_profile

router = APIRouter(prefix="/profile", tags=["profile"])
templates = Jinja2Templates(directory="app/templates")

TABS = ["personal", "experience", "projects", "skills", "education", "templates", "narrative"]


@router.get("", response_class=HTMLResponse)
def get_profile(request: Request, tab: str = "personal", db: Session = Depends(get_db)):
    if tab not in TABS:
        tab = "personal"
    profile = get_or_create_profile(db)
    db.commit()
    return templates.TemplateResponse(
        "profile/index.html",
        {"request": request, "profile": profile.data, "active_tab": tab},
    )
```

- [ ] **Step 5: Write `app/templates/profile/index.html`**

```html
{% extends "base.html" %}
{% block title %}Profile — JobApp{% endblock %}
{% block content %}
<h1 class="text-2xl font-bold mb-6">Profile</h1>

<!-- Tab nav -->
<div class="flex gap-1 border-b border-gray-200 mb-6">
  {% for tab in ["personal", "experience", "projects", "skills", "education", "templates", "narrative"] %}
  <a href="/profile?tab={{ tab }}"
     class="px-4 py-2 text-sm capitalize {{ 'tab-active' if active_tab == tab else 'text-gray-500 hover:text-gray-700' }}">
    {{ tab }}
  </a>
  {% endfor %}
</div>

<!-- Tab content -->
<div id="tab-content">
  {% if active_tab == "personal" %}
    {% include "profile/partials/personal.html" %}
  {% elif active_tab == "experience" %}
    {% include "profile/partials/experience.html" %}
  {% elif active_tab == "projects" %}
    {% include "profile/partials/projects.html" %}
  {% elif active_tab == "skills" %}
    {% include "profile/partials/skills.html" %}
  {% elif active_tab == "education" %}
    {% include "profile/partials/education.html" %}
  {% elif active_tab == "templates" %}
    {% include "profile/partials/templates_tab.html" %}
  {% elif active_tab == "narrative" %}
    {% include "profile/partials/narrative.html" %}
  {% endif %}
</div>
{% endblock %}
```

- [ ] **Step 6: Create `app/templates/profile/` directory and stub partials**

Create `app/templates/profile/partials/` directory and stub each partial so the template renders without errors:

```bash
mkdir -p app/templates/profile/partials
```

Create each file with a minimal placeholder (will be replaced in subsequent tasks):

`app/templates/profile/partials/personal.html`:
```html
<p class="text-gray-400 text-sm">Personal section — coming in next step.</p>
```

`app/templates/profile/partials/experience.html`:
```html
<p class="text-gray-400 text-sm">Experience section — coming in next step.</p>
```

`app/templates/profile/partials/projects.html`:
```html
<p class="text-gray-400 text-sm">Projects section.</p>
```

`app/templates/profile/partials/skills.html`:
```html
<p class="text-gray-400 text-sm">Skills section.</p>
```

`app/templates/profile/partials/education.html`:
```html
<p class="text-gray-400 text-sm">Education section.</p>
```

`app/templates/profile/partials/templates_tab.html`:
```html
<p class="text-gray-400 text-sm">Templates section.</p>
```

`app/templates/profile/partials/narrative.html`:
```html
<p class="text-gray-400 text-sm">Narrative section.</p>
```

- [ ] **Step 7: Register router in `app/main.py`**

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database import get_db
from app.routers import profile as profile_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="JobApp", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(profile_router.router)


@app.get("/health")
def health_check(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception:
        db_status = "error"
    return {"status": "ok", "db": db_status}


@app.get("/")
def root():
    return RedirectResponse(url="/apps")
```

- [ ] **Step 8: Run tests to verify they pass**

```bash
docker compose run --rm web pytest tests/test_profile_routes.py tests/test_health.py -v
```
Expected: `5 passed`

- [ ] **Step 9: Commit**

```bash
git add app/routers/ app/templates/profile/ app/main.py tests/test_profile_routes.py
git commit -m "feat: profile router + tabbed shell with stub partials"
```

---

## Task 6: Personal Tab

**Files:**
- Replace: `app/templates/profile/partials/personal.html`
- Add route to: `app/routers/profile.py`
- Add tests to: `tests/test_profile_routes.py`

- [ ] **Step 1: Add failing tests to `tests/test_profile_routes.py`**

Append these tests:

```python
def test_save_personal(client):
    response = client.post("/profile/personal", data={
        "name": "Jay Bhandari",
        "email": "jay@example.com",
        "phone": "555-1234",
        "linkedin": "linkedin.com/in/jay",
        "github": "github.com/jay",
        "location": "Boston, MA",
    })
    assert response.status_code == 200
    assert b"Jay Bhandari" in response.content


def test_save_personal_persists(client, db):
    client.post("/profile/personal", data={"name": "Persisted Jay", "email": "", "phone": "", "linkedin": "", "github": "", "location": ""})
    response = client.get("/profile?tab=personal")
    assert b"Persisted Jay" in response.content
```

- [ ] **Step 2: Run to verify they fail**

```bash
docker compose run --rm web pytest tests/test_profile_routes.py::test_save_personal -v
```
Expected: `405 Method Not Allowed`

- [ ] **Step 3: Add POST route to `app/routers/profile.py`**

Append to `app/routers/profile.py`:

```python
from fastapi import Form


@router.post("/personal", response_class=HTMLResponse)
def save_personal(
    request: Request,
    name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    linkedin: str = Form(""),
    github: str = Form(""),
    location: str = Form(""),
    db: Session = Depends(get_db),
):
    from app.services.profile_service import save_section
    profile = save_section(db, "personal", {
        "name": name, "email": email, "phone": phone,
        "linkedin": linkedin, "github": github, "location": location,
    })
    db.commit()
    return templates.TemplateResponse(
        "profile/partials/personal.html",
        {"request": request, "profile": profile.data, "saved": True},
    )
```

- [ ] **Step 4: Replace `app/templates/profile/partials/personal.html`**

```html
<form hx-post="/profile/personal" hx-target="this" hx-swap="outerHTML" class="space-y-4 max-w-xl">
  {% if saved %}
  <p class="text-green-600 text-sm success-flash">Saved.</p>
  {% endif %}

  <div>
    <label class="block text-sm font-medium text-gray-700">Name</label>
    <input type="text" name="name" value="{{ profile.personal.name }}"
           class="mt-1 block w-full border border-gray-300 rounded px-3 py-2 text-sm">
  </div>
  <div>
    <label class="block text-sm font-medium text-gray-700">Email</label>
    <input type="email" name="email" value="{{ profile.personal.email }}"
           class="mt-1 block w-full border border-gray-300 rounded px-3 py-2 text-sm">
  </div>
  <div>
    <label class="block text-sm font-medium text-gray-700">Phone</label>
    <input type="text" name="phone" value="{{ profile.personal.phone }}"
           class="mt-1 block w-full border border-gray-300 rounded px-3 py-2 text-sm">
  </div>
  <div>
    <label class="block text-sm font-medium text-gray-700">LinkedIn URL</label>
    <input type="text" name="linkedin" value="{{ profile.personal.linkedin }}"
           class="mt-1 block w-full border border-gray-300 rounded px-3 py-2 text-sm">
  </div>
  <div>
    <label class="block text-sm font-medium text-gray-700">GitHub URL</label>
    <input type="text" name="github" value="{{ profile.personal.github }}"
           class="mt-1 block w-full border border-gray-300 rounded px-3 py-2 text-sm">
  </div>
  <div>
    <label class="block text-sm font-medium text-gray-700">Location</label>
    <input type="text" name="location" value="{{ profile.personal.location }}"
           class="mt-1 block w-full border border-gray-300 rounded px-3 py-2 text-sm">
  </div>

  <button type="submit"
          class="bg-blue-600 text-white px-4 py-2 rounded text-sm hover:bg-blue-700">
    Save
    <span class="htmx-indicator ml-1">...</span>
  </button>
</form>
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
docker compose run --rm web pytest tests/test_profile_routes.py -v
```
Expected: `5 passed`

- [ ] **Step 6: Commit**

```bash
git add app/routers/profile.py app/templates/profile/partials/personal.html tests/test_profile_routes.py
git commit -m "feat: personal tab — save name, email, phone, linkedin, github, location"
```

---

## Task 7: Experience Tab

**Files:**
- Replace: `app/templates/profile/partials/experience.html`
- Create: `app/templates/profile/partials/experience_item.html`
- Add routes to: `app/routers/profile.py`
- Add tests to: `tests/test_profile_routes.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_profile_routes.py`:

```python
def test_add_experience(client):
    response = client.post("/profile/experience/add")
    assert response.status_code == 200
    assert b"experience-list" in response.content


def test_save_experience_item(client):
    # add first
    add_resp = client.post("/profile/experience/add")
    assert add_resp.status_code == 200
    # parse item id from response (it's in the HTML as data-id)
    import re
    match = re.search(r'data-id="([^"]+)"', add_resp.text)
    assert match, "No data-id found in response"
    item_id = match.group(1)

    save_resp = client.post(f"/profile/experience/{item_id}", data={
        "company": "Stripe",
        "role": "Software Engineer",
        "start_date": "2023-01",
        "end_date": "Present",
        "bullets": "Built payment APIs\nReduced latency by 40%",
        "tech": "Python, Go, PostgreSQL",
    })
    assert save_resp.status_code == 200
    assert b"Stripe" in save_resp.content


def test_delete_experience_item(client):
    add_resp = client.post("/profile/experience/add")
    import re
    item_id = re.search(r'data-id="([^"]+)"', add_resp.text).group(1)

    del_resp = client.delete(f"/profile/experience/{item_id}")
    assert del_resp.status_code == 200
    assert item_id.encode() not in del_resp.content
```

- [ ] **Step 2: Run to verify they fail**

```bash
docker compose run --rm web pytest tests/test_profile_routes.py::test_add_experience -v
```
Expected: `405`

- [ ] **Step 3: Add experience routes to `app/routers/profile.py`**

Append:

```python
@router.post("/experience/add", response_class=HTMLResponse)
def add_experience(request: Request, db: Session = Depends(get_db)):
    from app.services.profile_service import add_list_item
    profile = add_list_item(db, "experience", {
        "company": "", "role": "", "start_date": "", "end_date": "",
        "bullets": [], "tech": [],
    })
    db.commit()
    return templates.TemplateResponse(
        "profile/partials/experience.html",
        {"request": request, "profile": profile.data},
    )


@router.post("/experience/{item_id}", response_class=HTMLResponse)
def save_experience_item(
    request: Request,
    item_id: str,
    company: str = Form(""),
    role: str = Form(""),
    start_date: str = Form(""),
    end_date: str = Form(""),
    bullets: str = Form(""),
    tech: str = Form(""),
    db: Session = Depends(get_db),
):
    from app.services.profile_service import update_list_item
    bullets_list = [b.strip() for b in bullets.splitlines() if b.strip()]
    tech_list = [t.strip() for t in tech.split(",") if t.strip()]
    profile = update_list_item(db, "experience", item_id, {
        "company": company, "role": role, "start_date": start_date,
        "end_date": end_date, "bullets": bullets_list, "tech": tech_list,
    })
    db.commit()
    return templates.TemplateResponse(
        "profile/partials/experience.html",
        {"request": request, "profile": profile.data, "saved_id": item_id},
    )


@router.delete("/experience/{item_id}", response_class=HTMLResponse)
def delete_experience_item(request: Request, item_id: str, db: Session = Depends(get_db)):
    from app.services.profile_service import remove_list_item
    profile = remove_list_item(db, "experience", item_id)
    db.commit()
    return templates.TemplateResponse(
        "profile/partials/experience.html",
        {"request": request, "profile": profile.data},
    )
```

- [ ] **Step 4: Replace `app/templates/profile/partials/experience.html`**

```html
<div id="experience-list">
  {% for exp in profile.experience %}
    {% set saved = saved_id is defined and saved_id == exp.id %}
    <div data-id="{{ exp.id }}" class="border border-gray-200 rounded p-4 mb-3 bg-white">
      <details {% if not exp.company %}open{% endif %}>
        <summary class="cursor-pointer font-medium text-gray-700">
          {{ exp.company or "New Experience" }}{% if exp.role %} — {{ exp.role }}{% endif %}
          {% if saved %}<span class="text-green-600 text-xs ml-2 success-flash">Saved.</span>{% endif %}
        </summary>
        <form hx-post="/profile/experience/{{ exp.id }}"
              hx-target="#experience-list" hx-swap="outerHTML"
              class="mt-3 space-y-3">
          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="block text-xs text-gray-500">Company</label>
              <input type="text" name="company" value="{{ exp.company }}"
                     class="mt-1 w-full border border-gray-300 rounded px-2 py-1 text-sm">
            </div>
            <div>
              <label class="block text-xs text-gray-500">Role</label>
              <input type="text" name="role" value="{{ exp.role }}"
                     class="mt-1 w-full border border-gray-300 rounded px-2 py-1 text-sm">
            </div>
            <div>
              <label class="block text-xs text-gray-500">Start Date (YYYY-MM)</label>
              <input type="text" name="start_date" value="{{ exp.start_date }}"
                     class="mt-1 w-full border border-gray-300 rounded px-2 py-1 text-sm">
            </div>
            <div>
              <label class="block text-xs text-gray-500">End Date (YYYY-MM or Present)</label>
              <input type="text" name="end_date" value="{{ exp.end_date }}"
                     class="mt-1 w-full border border-gray-300 rounded px-2 py-1 text-sm">
            </div>
          </div>
          <div>
            <label class="block text-xs text-gray-500">Tech (comma-separated)</label>
            <input type="text" name="tech" value="{{ exp.tech | join(', ') }}"
                   class="mt-1 w-full border border-gray-300 rounded px-2 py-1 text-sm">
          </div>
          <div>
            <label class="block text-xs text-gray-500">Bullets (one per line)</label>
            <textarea name="bullets" rows="4"
                      class="mt-1 w-full border border-gray-300 rounded px-2 py-1 text-sm">{{ exp.bullets | join('\n') }}</textarea>
          </div>
          <div class="flex gap-2">
            <button type="submit"
                    class="bg-blue-600 text-white px-3 py-1 rounded text-sm hover:bg-blue-700">Save</button>
            <button type="button"
                    hx-delete="/profile/experience/{{ exp.id }}"
                    hx-target="#experience-list" hx-swap="outerHTML"
                    hx-confirm="Delete this experience entry?"
                    class="text-red-500 text-sm hover:underline">Delete</button>
          </div>
        </form>
      </details>
    </div>
  {% endfor %}

  <button hx-post="/profile/experience/add"
          hx-target="#experience-list" hx-swap="outerHTML"
          class="mt-2 text-blue-600 text-sm hover:underline">+ Add Experience</button>
</div>
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
docker compose run --rm web pytest tests/test_profile_routes.py -v
```
Expected: `8 passed`

- [ ] **Step 6: Commit**

```bash
git add app/routers/profile.py app/templates/profile/partials/experience.html tests/test_profile_routes.py
git commit -m "feat: experience tab — add, edit, delete with HTMX"
```

---

## Task 8: Projects + Skills + Education Tabs

**Files:**
- Replace: `app/templates/profile/partials/projects.html`
- Replace: `app/templates/profile/partials/skills.html`
- Replace: `app/templates/profile/partials/education.html`
- Add routes to: `app/routers/profile.py`
- Add tests to: `tests/test_profile_routes.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_profile_routes.py`:

```python
def test_add_project(client):
    response = client.post("/profile/projects/add")
    assert response.status_code == 200
    assert b"projects-list" in response.content


def test_save_skills(client):
    response = client.post("/profile/skills", data={
        "languages": "Python, Go",
        "frameworks": "FastAPI, React",
        "tools": "Docker, Git",
        "clouds": "AWS",
    })
    assert response.status_code == 200
    assert b"Python" in response.content


def test_add_education(client):
    response = client.post("/profile/education/add")
    assert response.status_code == 200
    assert b"education-list" in response.content
```

- [ ] **Step 2: Run to verify they fail**

```bash
docker compose run --rm web pytest tests/test_profile_routes.py::test_add_project tests/test_profile_routes.py::test_save_skills tests/test_profile_routes.py::test_add_education -v
```
Expected: `405`

- [ ] **Step 3: Add project, skills, education routes to `app/routers/profile.py`**

Append:

```python
# --- Projects ---

@router.post("/projects/add", response_class=HTMLResponse)
def add_project(request: Request, db: Session = Depends(get_db)):
    from app.services.profile_service import add_list_item
    profile = add_list_item(db, "projects", {
        "name": "", "description": "", "tech": [], "bullets": [], "url": "",
    })
    db.commit()
    return templates.TemplateResponse(
        "profile/partials/projects.html",
        {"request": request, "profile": profile.data},
    )


@router.post("/projects/{item_id}", response_class=HTMLResponse)
def save_project_item(
    request: Request,
    item_id: str,
    name: str = Form(""),
    description: str = Form(""),
    tech: str = Form(""),
    bullets: str = Form(""),
    url: str = Form(""),
    db: Session = Depends(get_db),
):
    from app.services.profile_service import update_list_item
    profile = update_list_item(db, "projects", item_id, {
        "name": name,
        "description": description,
        "tech": [t.strip() for t in tech.split(",") if t.strip()],
        "bullets": [b.strip() for b in bullets.splitlines() if b.strip()],
        "url": url,
    })
    db.commit()
    return templates.TemplateResponse(
        "profile/partials/projects.html",
        {"request": request, "profile": profile.data, "saved_id": item_id},
    )


@router.delete("/projects/{item_id}", response_class=HTMLResponse)
def delete_project_item(request: Request, item_id: str, db: Session = Depends(get_db)):
    from app.services.profile_service import remove_list_item
    profile = remove_list_item(db, "projects", item_id)
    db.commit()
    return templates.TemplateResponse(
        "profile/partials/projects.html",
        {"request": request, "profile": profile.data},
    )


# --- Skills ---

@router.post("/skills", response_class=HTMLResponse)
def save_skills(
    request: Request,
    languages: str = Form(""),
    frameworks: str = Form(""),
    tools: str = Form(""),
    clouds: str = Form(""),
    target_roles: str = Form(""),
    target_locations: str = Form(""),
    excluded_companies: str = Form(""),
    min_match_score: int = Form(70),
    db: Session = Depends(get_db),
):
    from app.services.profile_service import save_section, get_or_create_profile
    save_section(db, "skills", {
        "languages": [x.strip() for x in languages.split(",") if x.strip()],
        "frameworks": [x.strip() for x in frameworks.split(",") if x.strip()],
        "tools": [x.strip() for x in tools.split(",") if x.strip()],
        "clouds": [x.strip() for x in clouds.split(",") if x.strip()],
    })
    save_section(db, "target_roles", [x.strip() for x in target_roles.splitlines() if x.strip()])
    save_section(db, "target_locations", [x.strip() for x in target_locations.split(",") if x.strip()])
    save_section(db, "excluded_companies", [x.strip() for x in excluded_companies.splitlines() if x.strip()])
    profile = save_section(db, "min_match_score", min_match_score)
    db.commit()
    return templates.TemplateResponse(
        "profile/partials/skills.html",
        {"request": request, "profile": profile.data, "saved": True},
    )


# --- Education ---

@router.post("/education/add", response_class=HTMLResponse)
def add_education(request: Request, db: Session = Depends(get_db)):
    from app.services.profile_service import add_list_item
    profile = add_list_item(db, "education", {
        "school": "", "degree": "", "start_date": "", "end_date": "", "gpa": "",
    })
    db.commit()
    return templates.TemplateResponse(
        "profile/partials/education.html",
        {"request": request, "profile": profile.data},
    )


@router.post("/education/{item_id}", response_class=HTMLResponse)
def save_education_item(
    request: Request,
    item_id: str,
    school: str = Form(""),
    degree: str = Form(""),
    start_date: str = Form(""),
    end_date: str = Form(""),
    gpa: str = Form(""),
    db: Session = Depends(get_db),
):
    from app.services.profile_service import update_list_item
    profile = update_list_item(db, "education", item_id, {
        "school": school, "degree": degree,
        "start_date": start_date, "end_date": end_date, "gpa": gpa,
    })
    db.commit()
    return templates.TemplateResponse(
        "profile/partials/education.html",
        {"request": request, "profile": profile.data, "saved_id": item_id},
    )


@router.delete("/education/{item_id}", response_class=HTMLResponse)
def delete_education_item(request: Request, item_id: str, db: Session = Depends(get_db)):
    from app.services.profile_service import remove_list_item
    profile = remove_list_item(db, "education", item_id)
    db.commit()
    return templates.TemplateResponse(
        "profile/partials/education.html",
        {"request": request, "profile": profile.data},
    )
```

- [ ] **Step 4: Replace `app/templates/profile/partials/projects.html`**

```html
<div id="projects-list">
  {% for proj in profile.projects %}
  <div data-id="{{ proj.id }}" class="border border-gray-200 rounded p-4 mb-3 bg-white">
    <details {% if not proj.name %}open{% endif %}>
      <summary class="cursor-pointer font-medium text-gray-700">
        {{ proj.name or "New Project" }}
        {% if saved_id is defined and saved_id == proj.id %}
          <span class="text-green-600 text-xs ml-2 success-flash">Saved.</span>
        {% endif %}
      </summary>
      <form hx-post="/profile/projects/{{ proj.id }}"
            hx-target="#projects-list" hx-swap="outerHTML"
            class="mt-3 space-y-3">
        <div>
          <label class="block text-xs text-gray-500">Project Name</label>
          <input type="text" name="name" value="{{ proj.name }}"
                 class="mt-1 w-full border border-gray-300 rounded px-2 py-1 text-sm">
        </div>
        <div>
          <label class="block text-xs text-gray-500">Description</label>
          <textarea name="description" rows="2"
                    class="mt-1 w-full border border-gray-300 rounded px-2 py-1 text-sm">{{ proj.description }}</textarea>
        </div>
        <div>
          <label class="block text-xs text-gray-500">Tech (comma-separated)</label>
          <input type="text" name="tech" value="{{ proj.tech | join(', ') }}"
                 class="mt-1 w-full border border-gray-300 rounded px-2 py-1 text-sm">
        </div>
        <div>
          <label class="block text-xs text-gray-500">Bullets (one per line)</label>
          <textarea name="bullets" rows="3"
                    class="mt-1 w-full border border-gray-300 rounded px-2 py-1 text-sm">{{ proj.bullets | join('\n') }}</textarea>
        </div>
        <div>
          <label class="block text-xs text-gray-500">URL</label>
          <input type="text" name="url" value="{{ proj.url }}"
                 class="mt-1 w-full border border-gray-300 rounded px-2 py-1 text-sm">
        </div>
        <div class="flex gap-2">
          <button type="submit"
                  class="bg-blue-600 text-white px-3 py-1 rounded text-sm hover:bg-blue-700">Save</button>
          <button type="button"
                  hx-delete="/profile/projects/{{ proj.id }}"
                  hx-target="#projects-list" hx-swap="outerHTML"
                  hx-confirm="Delete this project?"
                  class="text-red-500 text-sm hover:underline">Delete</button>
        </div>
      </form>
    </details>
  </div>
  {% endfor %}
  <button hx-post="/profile/projects/add"
          hx-target="#projects-list" hx-swap="outerHTML"
          class="mt-2 text-blue-600 text-sm hover:underline">+ Add Project</button>
</div>
```

- [ ] **Step 5: Replace `app/templates/profile/partials/skills.html`**

```html
<form hx-post="/profile/skills" hx-target="this" hx-swap="outerHTML" class="space-y-5 max-w-xl">
  {% if saved %}
  <p class="text-green-600 text-sm success-flash">Saved.</p>
  {% endif %}

  <div>
    <label class="block text-sm font-medium text-gray-700">Languages <span class="text-gray-400 text-xs">(comma-separated)</span></label>
    <input type="text" name="languages" value="{{ profile.skills.languages | join(', ') }}"
           class="mt-1 block w-full border border-gray-300 rounded px-3 py-2 text-sm">
  </div>
  <div>
    <label class="block text-sm font-medium text-gray-700">Frameworks</label>
    <input type="text" name="frameworks" value="{{ profile.skills.frameworks | join(', ') }}"
           class="mt-1 block w-full border border-gray-300 rounded px-3 py-2 text-sm">
  </div>
  <div>
    <label class="block text-sm font-medium text-gray-700">Tools</label>
    <input type="text" name="tools" value="{{ profile.skills.tools | join(', ') }}"
           class="mt-1 block w-full border border-gray-300 rounded px-3 py-2 text-sm">
  </div>
  <div>
    <label class="block text-sm font-medium text-gray-700">Clouds</label>
    <input type="text" name="clouds" value="{{ profile.skills.clouds | join(', ') }}"
           class="mt-1 block w-full border border-gray-300 rounded px-3 py-2 text-sm">
  </div>

  <hr class="border-gray-200">

  <div>
    <label class="block text-sm font-medium text-gray-700">Target Roles <span class="text-gray-400 text-xs">(one per line)</span></label>
    <textarea name="target_roles" rows="3"
              class="mt-1 block w-full border border-gray-300 rounded px-3 py-2 text-sm">{{ profile.target_roles | join('\n') }}</textarea>
  </div>
  <div>
    <label class="block text-sm font-medium text-gray-700">Target Locations <span class="text-gray-400 text-xs">(comma-separated)</span></label>
    <input type="text" name="target_locations" value="{{ profile.target_locations | join(', ') }}"
           class="mt-1 block w-full border border-gray-300 rounded px-3 py-2 text-sm">
  </div>
  <div>
    <label class="block text-sm font-medium text-gray-700">Excluded Companies <span class="text-gray-400 text-xs">(one per line)</span></label>
    <textarea name="excluded_companies" rows="2"
              class="mt-1 block w-full border border-gray-300 rounded px-3 py-2 text-sm">{{ profile.excluded_companies | join('\n') }}</textarea>
  </div>
  <div>
    <label class="block text-sm font-medium text-gray-700">Min Match Score (0–100)</label>
    <input type="number" name="min_match_score" value="{{ profile.min_match_score }}" min="0" max="100"
           class="mt-1 w-24 border border-gray-300 rounded px-3 py-2 text-sm">
  </div>

  <button type="submit"
          class="bg-blue-600 text-white px-4 py-2 rounded text-sm hover:bg-blue-700">Save</button>
</form>
```

- [ ] **Step 6: Replace `app/templates/profile/partials/education.html`**

```html
<div id="education-list">
  {% for edu in profile.education %}
  <div data-id="{{ edu.id }}" class="border border-gray-200 rounded p-4 mb-3 bg-white">
    <details {% if not edu.school %}open{% endif %}>
      <summary class="cursor-pointer font-medium text-gray-700">
        {{ edu.school or "New Education" }}{% if edu.degree %} — {{ edu.degree }}{% endif %}
        {% if saved_id is defined and saved_id == edu.id %}
          <span class="text-green-600 text-xs ml-2 success-flash">Saved.</span>
        {% endif %}
      </summary>
      <form hx-post="/profile/education/{{ edu.id }}"
            hx-target="#education-list" hx-swap="outerHTML"
            class="mt-3 space-y-3">
        <div class="grid grid-cols-2 gap-3">
          <div>
            <label class="block text-xs text-gray-500">School</label>
            <input type="text" name="school" value="{{ edu.school }}"
                   class="mt-1 w-full border border-gray-300 rounded px-2 py-1 text-sm">
          </div>
          <div>
            <label class="block text-xs text-gray-500">Degree</label>
            <input type="text" name="degree" value="{{ edu.degree }}"
                   class="mt-1 w-full border border-gray-300 rounded px-2 py-1 text-sm">
          </div>
          <div>
            <label class="block text-xs text-gray-500">Start Date</label>
            <input type="text" name="start_date" value="{{ edu.start_date }}"
                   class="mt-1 w-full border border-gray-300 rounded px-2 py-1 text-sm">
          </div>
          <div>
            <label class="block text-xs text-gray-500">End Date</label>
            <input type="text" name="end_date" value="{{ edu.end_date }}"
                   class="mt-1 w-full border border-gray-300 rounded px-2 py-1 text-sm">
          </div>
          <div>
            <label class="block text-xs text-gray-500">GPA</label>
            <input type="text" name="gpa" value="{{ edu.gpa }}"
                   class="mt-1 w-full border border-gray-300 rounded px-2 py-1 text-sm">
          </div>
        </div>
        <div class="flex gap-2">
          <button type="submit"
                  class="bg-blue-600 text-white px-3 py-1 rounded text-sm hover:bg-blue-700">Save</button>
          <button type="button"
                  hx-delete="/profile/education/{{ edu.id }}"
                  hx-target="#education-list" hx-swap="outerHTML"
                  hx-confirm="Delete this education entry?"
                  class="text-red-500 text-sm hover:underline">Delete</button>
        </div>
      </form>
    </details>
  </div>
  {% endfor %}
  <button hx-post="/profile/education/add"
          hx-target="#education-list" hx-swap="outerHTML"
          class="mt-2 text-blue-600 text-sm hover:underline">+ Add Education</button>
</div>
```

- [ ] **Step 7: Run tests to verify they pass**

```bash
docker compose run --rm web pytest tests/test_profile_routes.py -v
```
Expected: `11 passed`

- [ ] **Step 8: Commit**

```bash
git add app/routers/profile.py app/templates/profile/partials/ tests/test_profile_routes.py
git commit -m "feat: projects, skills, education tabs with HTMX add/edit/delete"
```

---

## Task 9: Templates Tab

**Files:**
- Replace: `app/templates/profile/partials/templates_tab.html`
- Add route to: `app/routers/profile.py`
- Add test to: `tests/test_profile_routes.py`

- [ ] **Step 1: Add failing test**

Append to `tests/test_profile_routes.py`:

```python
def test_save_templates(client):
    response = client.post("/profile/templates", data={
        "latex_template": r"\documentclass{article}\begin{document}Hello\end{document}",
        "cover_letter_template": "Dear Hiring Manager,",
    })
    assert response.status_code == 200
    assert b"documentclass" in response.content
```

- [ ] **Step 2: Run to verify it fails**

```bash
docker compose run --rm web pytest tests/test_profile_routes.py::test_save_templates -v
```
Expected: `405`

- [ ] **Step 3: Add templates route to `app/routers/profile.py`**

Append:

```python
@router.post("/templates", response_class=HTMLResponse)
def save_templates(
    request: Request,
    latex_template: str = Form(""),
    cover_letter_template: str = Form(""),
    db: Session = Depends(get_db),
):
    from app.services.profile_service import save_section
    save_section(db, "latex_template", latex_template)
    profile = save_section(db, "cover_letter_template", cover_letter_template)
    db.commit()
    return templates.TemplateResponse(
        "profile/partials/templates_tab.html",
        {"request": request, "profile": profile.data, "saved": True},
    )
```

- [ ] **Step 4: Replace `app/templates/profile/partials/templates_tab.html`**

```html
<form hx-post="/profile/templates" hx-target="this" hx-swap="outerHTML" class="space-y-6">
  {% if saved %}
  <p class="text-green-600 text-sm success-flash">Saved.</p>
  {% endif %}

  <div>
    <label class="block text-sm font-medium text-gray-700 mb-1">
      LaTeX Resume Template
      <span class="text-gray-400 text-xs font-normal ml-1">— use <code>%%CONTENT%%</code> placeholders where tailored sections inject</span>
    </label>
    <textarea name="latex_template" rows="20"
              class="w-full border border-gray-300 rounded px-3 py-2 text-sm latex-editor">{{ profile.latex_template }}</textarea>
  </div>

  <div>
    <label class="block text-sm font-medium text-gray-700 mb-1">
      Cover Letter Base Template
      <span class="text-gray-400 text-xs font-normal ml-1">— LLM rewrites this with job-specific content</span>
    </label>
    <textarea name="cover_letter_template" rows="10"
              class="w-full border border-gray-300 rounded px-3 py-2 text-sm">{{ profile.cover_letter_template }}</textarea>
  </div>

  <button type="submit"
          class="bg-blue-600 text-white px-4 py-2 rounded text-sm hover:bg-blue-700">Save Templates</button>
</form>
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
docker compose run --rm web pytest tests/test_profile_routes.py -v
```
Expected: `12 passed`

- [ ] **Step 6: Commit**

```bash
git add app/routers/profile.py app/templates/profile/partials/templates_tab.html tests/test_profile_routes.py
git commit -m "feat: templates tab — LaTeX and cover letter template editors"
```

---

## Task 10: Narrative Service Functions

**Files:**
- Add to: `app/services/profile_service.py` — `generate_questions`, `generate_summary`, `save_narrative_answer`
- Add tests to: `tests/test_profile_service.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_profile_service.py`:

```python
from unittest.mock import patch


def test_save_narrative_answer(db):
    from app.services.profile_service import save_narrative_answer, get_or_create_profile

    profile = get_or_create_profile(db)
    # seed questions first
    from app.services.profile_service import save_section
    save_section(db, "narrative", {
        "answers": [
            {"question": "How do colleagues describe your problem-solving style?", "answer": ""},
            {"question": "What energizes you at work?", "answer": ""},
        ],
        "summary": "",
    })
    db.flush()

    updated = save_narrative_answer(db, index=0, answer="People come to me when stuck.")
    assert updated.data["narrative"]["answers"][0]["answer"] == "People come to me when stuck."
    assert updated.data["narrative"]["answers"][1]["answer"] == ""  # untouched


def test_generate_questions_calls_llm(db):
    from app.services.profile_service import generate_questions

    mock_content = """1. How do colleagues describe your problem-solving style?
2. What kinds of problems energize you most?
3. How do you handle ambiguity?"""

    with patch("app.services.profile_service.chat_completion", return_value=mock_content):
        profile = generate_questions(db, api_key="k", base_url="https://api", model="test/model")

    answers = profile.data["narrative"]["answers"]
    assert len(answers) == 3
    assert answers[0]["question"] == "How do colleagues describe your problem-solving style?"
    assert answers[0]["answer"] == ""


def test_generate_summary_calls_llm(db):
    from app.services.profile_service import generate_summary, save_section, get_or_create_profile

    profile = get_or_create_profile(db)
    save_section(db, "narrative", {
        "answers": [
            {"question": "Q1", "answer": "I'm a fast learner."},
            {"question": "Q2", "answer": "I love solving hard problems."},
        ],
        "summary": "",
    })
    db.flush()

    with patch("app.services.profile_service.chat_completion", return_value="Jay is a fast learner who loves hard problems."):
        updated = generate_summary(db, api_key="k", base_url="https://api", model="test/model")

    assert updated.data["narrative"]["summary"] == "Jay is a fast learner who loves hard problems."
```

- [ ] **Step 2: Run to verify they fail**

```bash
docker compose run --rm web pytest tests/test_profile_service.py::test_save_narrative_answer tests/test_profile_service.py::test_generate_questions_calls_llm -v
```
Expected: `ImportError`

- [ ] **Step 3: Append to `app/services/profile_service.py`**

```python
import copy
import re
from app.llm.client import chat_completion


def save_narrative_answer(db: Session, index: int, answer: str) -> Profile:
    profile = get_or_create_profile(db)
    updated = copy.deepcopy(profile.data)
    if 0 <= index < len(updated["narrative"]["answers"]):
        updated["narrative"]["answers"][index]["answer"] = answer
    profile.data = updated
    db.flush()
    return profile


def generate_questions(db: Session, api_key: str, base_url: str, model: str) -> Profile:
    prompt = """Generate exactly 15 thoughtful questions to understand a software engineer's
personality, work style, strengths, and unique value. These answers will be used to write
personalized resumes and cover letters that sound authentically like the person.

Cover: problem-solving style, how colleagues rely on them, what energizes them, handling
ambiguity, proudest technical moment, learning style, leadership/collaboration style,
what makes them different, what they want employers to know that their resume doesn't show.

Format: numbered list only, one question per line, no extra text.
Example format:
1. How do colleagues describe your problem-solving style?
2. What kinds of problems energize you most?"""

    response = chat_completion(
        messages=[{"role": "user", "content": prompt}],
        api_key=api_key,
        base_url=base_url,
        model=model,
    )

    questions = []
    for line in response.strip().splitlines():
        line = line.strip()
        cleaned = re.sub(r"^\d+\.\s*", "", line)
        if cleaned:
            questions.append({"question": cleaned, "answer": ""})

    updated_narrative = {
        "answers": questions,
        "summary": "",
    }
    return save_section(db, "narrative", updated_narrative)


def generate_summary(db: Session, api_key: str, base_url: str, model: str) -> Profile:
    profile = get_or_create_profile(db)
    answers = profile.data["narrative"]["answers"]

    qa_text = "\n".join(
        f"Q: {item['question']}\nA: {item['answer']}"
        for item in answers
        if item.get("answer", "").strip()
    )

    if not qa_text:
        return profile

    prompt = f"""Based on these Q&A answers from a software engineer, write a 2-3 sentence
first-person narrative summary that captures their personality, work style, and unique value.
It should sound natural and personal — not like a resume summary.

{qa_text}

Write only the summary paragraph. No intro, no labels."""

    summary = chat_completion(
        messages=[{"role": "user", "content": prompt}],
        api_key=api_key,
        base_url=base_url,
        model=model,
        temperature=0.8,
        max_tokens=300,
    )

    updated = copy.deepcopy(profile.data)
    updated["narrative"]["summary"] = summary.strip()
    profile.data = updated
    db.flush()
    return profile
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
docker compose run --rm web pytest tests/test_profile_service.py -v
```
Expected: `10 passed`

- [ ] **Step 5: Commit**

```bash
git add app/services/profile_service.py tests/test_profile_service.py
git commit -m "feat: narrative service — save answers, generate questions, generate summary via LLM"
```

---

## Task 11: Narrative Tab UI

**Files:**
- Replace: `app/templates/profile/partials/narrative.html`
- Create: `app/templates/profile/partials/narrative_answer.html`
- Add routes to: `app/routers/profile.py`
- Add tests to: `tests/test_profile_routes.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_profile_routes.py`:

```python
def test_save_narrative_answer_route(client, db):
    from app.services.profile_service import save_section, get_or_create_profile
    profile = get_or_create_profile(db)
    save_section(db, "narrative", {
        "answers": [{"question": "How do you solve problems?", "answer": ""}],
        "summary": "",
    })
    db.commit()

    response = client.post("/profile/narrative/answer/0", data={"answer": "I break it down."})
    assert response.status_code == 200
    assert b"I break it down." in response.content


def test_generate_questions_route(client):
    from unittest.mock import patch
    mock_qs = "1. How do you solve problems?\n2. What energizes you?"
    with patch("app.routers.profile.chat_completion", return_value=mock_qs):
        response = client.post("/profile/narrative/generate-questions")
    assert response.status_code == 200


def test_regenerate_summary_route(client, db):
    from unittest.mock import patch
    from app.services.profile_service import save_section, get_or_create_profile
    profile = get_or_create_profile(db)
    save_section(db, "narrative", {
        "answers": [{"question": "Q", "answer": "I love hard problems."}],
        "summary": "",
    })
    db.commit()

    with patch("app.routers.profile.chat_completion", return_value="Jay loves hard problems."):
        response = client.post("/profile/narrative/regenerate-summary")
    assert response.status_code == 200
    assert b"Jay loves hard problems." in response.content
```

- [ ] **Step 2: Run to verify they fail**

```bash
docker compose run --rm web pytest tests/test_profile_routes.py::test_save_narrative_answer_route -v
```
Expected: `405`

- [ ] **Step 3: Add narrative routes to `app/routers/profile.py`**

Add at the top of the file, update import:
```python
from app.llm.client import chat_completion
```

Append routes:

```python
@router.post("/narrative/generate-questions", response_class=HTMLResponse)
def narrative_generate_questions(request: Request, db: Session = Depends(get_db)):
    from app.services.profile_service import generate_questions
    from app.config import settings
    profile = generate_questions(
        db,
        api_key=settings.NVIDIA_NIM_API_KEY,
        base_url=settings.NVIDIA_NIM_BASE_URL,
        model=settings.NVIDIA_NIM_MODEL,
    )
    db.commit()
    return templates.TemplateResponse(
        "profile/partials/narrative.html",
        {"request": request, "profile": profile.data},
    )


@router.post("/narrative/answer/{index}", response_class=HTMLResponse)
def save_narrative_answer(
    request: Request,
    index: int,
    answer: str = Form(""),
    db: Session = Depends(get_db),
):
    from app.services.profile_service import save_narrative_answer as svc_save
    profile = svc_save(db, index=index, answer=answer)
    db.commit()
    item = profile.data["narrative"]["answers"][index]
    return templates.TemplateResponse(
        "profile/partials/narrative_answer.html",
        {"request": request, "item": item, "index": index, "saved": True},
    )


@router.post("/narrative/regenerate-summary", response_class=HTMLResponse)
def regenerate_summary(request: Request, db: Session = Depends(get_db)):
    from app.services.profile_service import generate_summary
    from app.config import settings
    profile = generate_summary(
        db,
        api_key=settings.NVIDIA_NIM_API_KEY,
        base_url=settings.NVIDIA_NIM_BASE_URL,
        model=settings.NVIDIA_NIM_MODEL,
    )
    db.commit()
    return templates.TemplateResponse(
        "profile/partials/narrative.html",
        {"request": request, "profile": profile.data},
    )
```

- [ ] **Step 4: Replace `app/templates/profile/partials/narrative.html`**

```html
<div id="narrative-panel" class="space-y-6">

  {% if not profile.narrative.answers %}
  <!-- No questions yet -->
  <div class="text-center py-12">
    <p class="text-gray-500 mb-4">No questions yet. Generate questions to get started.</p>
    <button hx-post="/profile/narrative/generate-questions"
            hx-target="#narrative-panel" hx-swap="outerHTML"
            hx-indicator="#gen-spinner"
            class="bg-blue-600 text-white px-5 py-2 rounded hover:bg-blue-700">
      Generate Questions
    </button>
    <span id="gen-spinner" class="htmx-indicator ml-2 text-gray-400 text-sm">Generating...</span>
  </div>

  {% else %}
  <!-- Questions + answers -->
  <div class="flex justify-between items-center">
    <h2 class="text-lg font-semibold">Narrative Questions</h2>
    <button hx-post="/profile/narrative/generate-questions"
            hx-target="#narrative-panel" hx-swap="outerHTML"
            hx-confirm="This will reset all answers. Continue?"
            class="text-sm text-gray-500 hover:text-blue-600">Regenerate Questions</button>
  </div>

  <div class="space-y-4" id="answers-list">
    {% for item in profile.narrative.answers %}
      {% set loop_index = loop.index0 %}
      {% include "profile/partials/narrative_answer.html" %}
    {% endfor %}
  </div>

  <!-- Summary -->
  <div class="border-t border-gray-200 pt-6">
    <div class="flex justify-between items-center mb-3">
      <h3 class="font-semibold text-gray-700">Voice Summary</h3>
      <button hx-post="/profile/narrative/regenerate-summary"
              hx-target="#narrative-panel" hx-swap="outerHTML"
              hx-indicator="#summary-spinner"
              class="text-sm text-blue-600 hover:underline">
        Regenerate Summary
        <span id="summary-spinner" class="htmx-indicator text-gray-400">...</span>
      </button>
    </div>
    {% if profile.narrative.summary %}
    <p class="text-gray-700 text-sm bg-blue-50 border border-blue-100 rounded p-4 leading-relaxed">
      {{ profile.narrative.summary }}
    </p>
    {% else %}
    <p class="text-gray-400 text-sm">Answer the questions above, then click Regenerate Summary.</p>
    {% endif %}
  </div>

  {% endif %}
</div>
```

- [ ] **Step 5: Create `app/templates/profile/partials/narrative_answer.html`**

```html
{% set idx = loop_index if loop_index is defined else index %}
<div class="border border-gray-100 rounded p-4 bg-white" id="answer-{{ idx }}">
  <p class="text-sm font-medium text-gray-700 mb-2">{{ idx + 1 }}. {{ item.question }}</p>
  <form hx-post="/profile/narrative/answer/{{ idx }}"
        hx-target="#answer-{{ idx }}" hx-swap="outerHTML">
    {% if saved %}
    <p class="text-green-600 text-xs mb-1 success-flash">Saved.</p>
    {% endif %}
    <textarea name="answer" rows="3"
              class="w-full border border-gray-200 rounded px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-300">{{ item.answer }}</textarea>
    <button type="submit"
            class="mt-2 text-xs text-blue-600 hover:underline">Save answer</button>
  </form>
</div>
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
docker compose run --rm web pytest tests/test_profile_routes.py -v
```
Expected: `15 passed`

- [ ] **Step 7: Commit**

```bash
git add app/routers/profile.py app/templates/profile/partials/ tests/test_profile_routes.py
git commit -m "feat: narrative tab — generate questions, save answers, regenerate summary via LLM"
```

---

## Task 12: Full Test Suite Pass

- [ ] **Step 1: Run all tests**

```bash
docker compose run --rm web pytest tests/ -v --tb=short
```

Expected counts:
- `test_config.py`: 2
- `test_models_job.py`: 3
- `test_models_profile.py`: 2
- `test_models_application.py`: 4
- `test_health.py`: 2
- `test_celery.py`: 2
- `test_llm_client.py`: 2
- `test_profile_service.py`: 10
- `test_profile_routes.py`: 15
- **Total: 42 passed**

- [ ] **Step 2: Fix any failures before proceeding**

- [ ] **Step 3: Final commit**

```bash
git add -A
git commit -m "chore: plan 02 profile & narrative complete — 42 tests passing"
```

---

## What Plan 3 Builds

Plan 3 (Job Fetching) adds:
- Celery task `fetch_jobs` triggered every 5hr via Celery Beat
- API source adapters: Adzuna, JSearch (RapidAPI), Greenhouse/Lever direct endpoints
- Playwright scrapers: LinkedIn, Indeed, Wellfound, Dice
- Three-layer deduplication (URL → source_job_id → content hash)
- Jobs stored in the `jobs` table, visible in `/jobs` route (stub)

Plan 4 (Job Matching) depends on Plan 3's job data.
