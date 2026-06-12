# Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the complete infrastructure foundation — Docker Compose stack, PostgreSQL schema, SQLAlchemy models, Alembic migrations, FastAPI skeleton, and Celery app — that all subsequent plans build on.

**Architecture:** Modular monolith in Docker Compose with 5 services (`web`, `worker`, `beat`, `postgres`, `redis`). SQLAlchemy 2.x models with Alembic migrations manage the schema. FastAPI serves the web UI on port 8000. Celery workers handle async background tasks via Redis broker.

**Tech Stack:** Python 3.12, FastAPI 0.115, SQLAlchemy 2.0, Alembic 1.13, Celery 5.4, Redis 5, PostgreSQL 16, Docker Compose v2, pytest 8, httpx 0.27

---

## File Map

```
jobapp/
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── .gitignore
├── Makefile
├── pyproject.toml
├── alembic.ini
├── alembic/
│   ├── env.py
│   └── versions/
│       └── 0001_initial.py
├── nginx/
│   └── nginx.conf
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI app instance, router registration, lifespan
│   ├── config.py            # Pydantic Settings — all env vars in one place
│   ├── database.py          # SQLAlchemy engine, SessionLocal, get_db dependency
│   ├── celery_app.py        # Celery instance with Redis broker/backend
│   └── models/
│       ├── __init__.py      # re-exports all models (needed by Alembic)
│       ├── job.py           # Job model + JobStatus enum
│       ├── profile.py       # Profile model (single-row JSON store)
│       └── application.py   # Application, ApplicationDocument models + enums
└── tests/
    ├── __init__.py
    ├── conftest.py          # engine, session, client fixtures against test DB
    ├── test_config.py
    ├── test_models_job.py
    ├── test_models_profile.py
    ├── test_models_application.py
    └── test_health.py
```

---

## Task 1: Project Scaffold

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `.env.example`
- Create: `Makefile`
- Create: `app/__init__.py`
- Create: `app/models/__init__.py`
- Create: `tests/__init__.py`

- [ ] **Step 1: Create directory structure**

```bash
mkdir -p app/models alembic/versions nginx tests
touch app/__init__.py app/models/__init__.py tests/__init__.py
```

- [ ] **Step 2: Write `pyproject.toml`**

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
]

[project.optional-dependencies]
dev = [
    "pytest==8.3.4",
    "pytest-asyncio==0.24.0",
    "pytest-cov==6.0.0",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 3: Write `.gitignore`**

```
__pycache__/
*.pyc
*.pyo
.env
.venv/
venv/
*.egg-info/
dist/
.pytest_cache/
.coverage
htmlcov/
storage/
alembic/versions/*.py
!alembic/versions/.gitkeep
```

- [ ] **Step 4: Write `.env.example`**

```bash
# Database
DATABASE_URL=postgresql://jobapp:jobapp@postgres:5432/jobapp
TEST_DATABASE_URL=postgresql://jobapp:jobapp@postgres:5432/jobapp_test

# Redis
REDIS_URL=redis://redis:6379/0

# LLM - NVIDIA NIM (OpenAI-compatible)
NVIDIA_NIM_API_KEY=your_key_here
NVIDIA_NIM_BASE_URL=https://integrate.api.nvidia.com/v1
NVIDIA_NIM_MODEL=meta/llama-3.1-70b-instruct

# Outreach (optional)
HUNTER_IO_API_KEY=

# App
SECRET_KEY=change-me-in-production
DEBUG=false
STORAGE_PATH=/storage
MIN_MATCH_SCORE=70
FETCH_INTERVAL_HOURS=5
MIN_KEYWORD_SKILLS=2
```

- [ ] **Step 5: Write `Makefile`**

```makefile
.PHONY: up down logs build test migrate shell

up:
	docker compose up -d

down:
	docker compose down

build:
	docker compose build

logs:
	docker compose logs -f

test:
	docker compose run --rm web pytest tests/ -v

migrate:
	docker compose run --rm web alembic upgrade head

shell:
	docker compose run --rm web python

lint:
	docker compose run --rm web python -m py_compile app/**/*.py
```

- [ ] **Step 6: Commit**

```bash
git init
git add pyproject.toml .gitignore .env.example Makefile app/ tests/
git commit -m "chore: project scaffold"
```

---

## Task 2: Docker Compose Stack

**Files:**
- Create: `Dockerfile`
- Create: `docker-compose.yml`
- Create: `nginx/nginx.conf`

- [ ] **Step 1: Write `Dockerfile`**

```dockerfile
FROM python:3.12-slim

# Install system deps: pdflatex + playwright deps
RUN apt-get update && apt-get install -y \
    texlive-latex-base \
    texlive-fonts-recommended \
    texlive-latex-extra \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[dev]"

# Install playwright chromium for scraping
RUN playwright install chromium --with-deps

COPY . .

RUN mkdir -p /storage/resumes /storage/cover_letters /storage/tex
```

- [ ] **Step 2: Write `docker-compose.yml`**

```yaml
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: jobapp
      POSTGRES_PASSWORD: jobapp
      POSTGRES_DB: jobapp
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U jobapp"]
      interval: 5s
      timeout: 5s
      retries: 5

  redis:
    image: redis:7-alpine
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 5s
      retries: 5

  web:
    build: .
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
    volumes:
      - .:/app
      - storage_data:/storage
    ports:
      - "8000:8000"
    env_file: .env
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy

  worker:
    build: .
    command: celery -A app.celery_app worker --loglevel=info --concurrency=4
    volumes:
      - .:/app
      - storage_data:/storage
    env_file: .env
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy

  beat:
    build: .
    command: celery -A app.celery_app beat --loglevel=info
    volumes:
      - .:/app
    env_file: .env
    depends_on:
      - redis

  nginx:
    image: nginx:alpine
    ports:
      - "80:80"
    volumes:
      - ./nginx/nginx.conf:/etc/nginx/conf.d/default.conf
    depends_on:
      - web

volumes:
  postgres_data:
  storage_data:
```

- [ ] **Step 3: Write `nginx/nginx.conf`**

```nginx
server {
    listen 80;

    client_max_body_size 10M;

    location / {
        proxy_pass http://web:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    location /storage/ {
        alias /storage/;
        add_header Content-Disposition 'attachment';
    }
}
```

- [ ] **Step 4: Copy `.env.example` to `.env` and fill in values**

```bash
cp .env.example .env
# Edit .env — set NVIDIA_NIM_API_KEY at minimum
```

- [ ] **Step 5: Build and start**

```bash
docker compose build
docker compose up -d
```

Expected: all 5 containers running. Check with:
```bash
docker compose ps
```
Expected output: all services show `healthy` or `running`.

- [ ] **Step 6: Commit**

```bash
git add Dockerfile docker-compose.yml nginx/
git commit -m "chore: docker compose stack with postgres, redis, nginx, worker, beat"
```

---

## Task 3: App Configuration

**Files:**
- Create: `app/config.py`
- Create: `tests/test_config.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_config.py
import os
import pytest
from app.config import Settings


def test_settings_load_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("SECRET_KEY", "testsecret")
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "testkey")
    monkeypatch.setenv("NVIDIA_NIM_BASE_URL", "https://api.nvidia.com/v1")
    monkeypatch.setenv("NVIDIA_NIM_MODEL", "meta/llama-3.1-70b-instruct")

    settings = Settings()

    assert settings.DATABASE_URL == "postgresql://u:p@localhost/db"
    assert settings.REDIS_URL == "redis://localhost:6379/0"
    assert settings.MIN_MATCH_SCORE == 70
    assert settings.MIN_KEYWORD_SKILLS == 2
    assert settings.FETCH_INTERVAL_HOURS == 5


def test_settings_defaults():
    s = Settings(
        DATABASE_URL="postgresql://u:p@localhost/db",
        REDIS_URL="redis://localhost:6379/0",
        SECRET_KEY="s",
        NVIDIA_NIM_API_KEY="k",
        NVIDIA_NIM_BASE_URL="https://api.nvidia.com/v1",
        NVIDIA_NIM_MODEL="meta/llama-3.1-70b-instruct",
    )
    assert s.MIN_MATCH_SCORE == 70
    assert s.STORAGE_PATH == "/storage"
    assert s.DEBUG is False
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker compose run --rm web pytest tests/test_config.py -v
```
Expected: `ImportError: cannot import name 'Settings' from 'app.config'`

- [ ] **Step 3: Write `app/config.py`**

```python
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str
    TEST_DATABASE_URL: str = ""
    REDIS_URL: str
    SECRET_KEY: str

    NVIDIA_NIM_API_KEY: str
    NVIDIA_NIM_BASE_URL: str
    NVIDIA_NIM_MODEL: str = "meta/llama-3.1-70b-instruct"

    HUNTER_IO_API_KEY: str = ""

    DEBUG: bool = False
    STORAGE_PATH: str = "/storage"
    MIN_MATCH_SCORE: int = 70
    FETCH_INTERVAL_HOURS: int = 5
    MIN_KEYWORD_SKILLS: int = 2


settings = Settings()
```

- [ ] **Step 4: Run test to verify it passes**

```bash
docker compose run --rm web pytest tests/test_config.py -v
```
Expected: `2 passed`

- [ ] **Step 5: Commit**

```bash
git add app/config.py tests/test_config.py
git commit -m "feat: app configuration with pydantic-settings"
```

---

## Task 4: Database Connection

**Files:**
- Create: `app/database.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Write `app/database.py`**

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from app.config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
```

- [ ] **Step 2: Write `tests/conftest.py`**

```python
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

from app.database import Base
from app.config import settings
import app.models  # noqa: F401 — registers all models with Base.metadata before create_all

TEST_DB_URL = settings.TEST_DATABASE_URL or settings.DATABASE_URL.replace(
    "/jobapp", "/jobapp_test"
)

test_engine = create_engine(TEST_DB_URL, pool_pre_ping=True)
TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


@pytest.fixture(scope="session", autouse=True)
def setup_test_db():
    Base.metadata.create_all(bind=test_engine)
    yield
    Base.metadata.drop_all(bind=test_engine)


@pytest.fixture
def db():
    session = TestSessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def client(db):
    from app.main import app
    from app.database import get_db

    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
```

- [ ] **Step 3: Create the test database inside the postgres container**

```bash
docker compose exec postgres psql -U jobapp -c "CREATE DATABASE jobapp_test;"
```
Expected: `CREATE DATABASE`

- [ ] **Step 4: Verify connection works**

```bash
docker compose run --rm web python -c "
from app.database import engine
with engine.connect() as conn:
    print('DB connection OK')
"
```
Expected: `DB connection OK`

- [ ] **Step 5: Commit**

```bash
git add app/database.py tests/conftest.py
git commit -m "feat: sqlalchemy engine, session, and test fixtures"
```

---

## Task 5: Job Model

**Files:**
- Create: `app/models/job.py`
- Create: `tests/test_models_job.py`
- Modify: `app/models/__init__.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_models_job.py
import pytest
from datetime import datetime, timezone
from app.models.job import Job, JobStatus


def test_create_job(db):
    job = Job(
        source="adzuna",
        title="Software Engineer",
        company="Stripe",
        location="New York",
        url="https://example.com/job/123",
        description="We are looking for a SWE...",
        fetched_at=datetime.now(timezone.utc),
        dedupe_hash="abc123",
    )
    db.add(job)
    db.flush()

    assert job.id is not None
    assert job.status == JobStatus.new
    assert job.is_remote is False
    assert job.source_urls == []
    assert job.matched_skills == []
    assert job.missing_skills == []


def test_job_status_enum(db):
    job = Job(
        source="linkedin",
        title="Backend Engineer",
        company="Acme",
        url="https://example.com/job/456",
        description="...",
        fetched_at=datetime.now(timezone.utc),
        dedupe_hash="def456",
        status=JobStatus.matched,
    )
    db.add(job)
    db.flush()

    fetched = db.query(Job).filter_by(id=job.id).first()
    assert fetched.status == JobStatus.matched


def test_job_dedupe_hash_unique(db):
    from sqlalchemy.exc import IntegrityError

    job1 = Job(
        source="indeed",
        title="SWE",
        company="Corp",
        url="https://example.com/1",
        description="...",
        fetched_at=datetime.now(timezone.utc),
        dedupe_hash="samehash",
    )
    job2 = Job(
        source="linkedin",
        title="SWE",
        company="Corp",
        url="https://example.com/2",
        description="...",
        fetched_at=datetime.now(timezone.utc),
        dedupe_hash="samehash",
    )
    db.add(job1)
    db.flush()
    db.add(job2)

    with pytest.raises(IntegrityError):
        db.flush()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker compose run --rm web pytest tests/test_models_job.py -v
```
Expected: `ImportError: cannot import name 'Job'`

- [ ] **Step 3: Write `app/models/job.py`**

```python
import uuid
import enum
from datetime import datetime, timezone

from sqlalchemy import String, Boolean, Float, Text, DateTime, Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID, ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class JobStatus(enum.Enum):
    new = "new"
    filtered_out = "filtered_out"
    matched = "matched"
    docs_generated = "docs_generated"


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source: Mapped[str] = mapped_column(String, nullable=False)
    source_job_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_urls: Mapped[list] = mapped_column(ARRAY(String), default=list)
    title: Mapped[str] = mapped_column(String, nullable=False)
    company: Mapped[str] = mapped_column(String, nullable=False)
    location: Mapped[str | None] = mapped_column(String, nullable=True)
    is_remote: Mapped[bool] = mapped_column(Boolean, default=False)
    url: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    experience_level: Mapped[str | None] = mapped_column(String, nullable=True)
    keyword_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    llm_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    llm_reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    matched_skills: Mapped[list] = mapped_column(ARRAY(String), default=list)
    missing_skills: Mapped[list] = mapped_column(ARRAY(String), default=list)
    status: Mapped[JobStatus] = mapped_column(
        SAEnum(JobStatus), default=JobStatus.new, nullable=False
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    dedupe_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
```

- [ ] **Step 4: Update `app/models/__init__.py`**

```python
from app.models.job import Job, JobStatus
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
docker compose run --rm web pytest tests/test_models_job.py -v
```
Expected: `3 passed`

- [ ] **Step 6: Commit**

```bash
git add app/models/job.py app/models/__init__.py tests/test_models_job.py
git commit -m "feat: Job model with JobStatus enum and dedupe constraint"
```

---

## Task 6: Profile Model

**Files:**
- Create: `app/models/profile.py`
- Create: `tests/test_models_profile.py`
- Modify: `app/models/__init__.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_models_profile.py
from app.models.profile import Profile


def test_create_profile(db):
    profile = Profile(data={
        "personal": {"name": "Jay", "email": "jay@example.com"},
        "experience": [],
        "projects": [],
        "skills": {"languages": ["Python"], "frameworks": [], "tools": [], "clouds": []},
        "education": [],
        "target_roles": ["Software Engineer"],
        "target_locations": ["Remote"],
        "excluded_companies": [],
        "min_match_score": 70,
        "narrative": {"answers": [], "summary": ""},
    })
    db.add(profile)
    db.flush()

    assert profile.id is not None
    assert profile.data["personal"]["name"] == "Jay"
    assert profile.updated_at is not None


def test_profile_data_update(db):
    profile = Profile(data={"personal": {"name": "Jay"}})
    db.add(profile)
    db.flush()

    profile.data = {**profile.data, "personal": {"name": "Jay Updated"}}
    db.flush()

    fetched = db.query(Profile).filter_by(id=profile.id).first()
    assert fetched.data["personal"]["name"] == "Jay Updated"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker compose run --rm web pytest tests/test_models_profile.py -v
```
Expected: `ImportError: cannot import name 'Profile'`

- [ ] **Step 3: Write `app/models/profile.py`**

```python
from datetime import datetime

from sqlalchemy import Integer, DateTime, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Profile(Base):
    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
```

- [ ] **Step 4: Update `app/models/__init__.py`**

```python
from app.models.job import Job, JobStatus
from app.models.profile import Profile
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
docker compose run --rm web pytest tests/test_models_profile.py -v
```
Expected: `2 passed`

- [ ] **Step 6: Commit**

```bash
git add app/models/profile.py app/models/__init__.py tests/test_models_profile.py
git commit -m "feat: Profile model as single-row JSONB store"
```

---

## Task 7: Application and ApplicationDocument Models

**Files:**
- Create: `app/models/application.py`
- Create: `tests/test_models_application.py`
- Modify: `app/models/__init__.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_models_application.py
import pytest
from datetime import datetime, timezone
from app.models.job import Job, JobStatus
from app.models.application import Application, ApplicationDocument, ApplicationStatus, DocType


def _make_job(db, suffix="1"):
    job = Job(
        source="adzuna",
        title="SWE",
        company="Acme",
        url=f"https://example.com/job/{suffix}",
        description="...",
        fetched_at=datetime.now(timezone.utc),
        dedupe_hash=f"hash{suffix}",
        status=JobStatus.docs_generated,
    )
    db.add(job)
    db.flush()
    return job


def test_create_application(db):
    job = _make_job(db, "a1")
    app = Application(job_id=job.id)
    db.add(app)
    db.flush()

    assert app.id is not None
    assert app.status == ApplicationStatus.not_applied
    assert app.outreach_contacts == []
    assert app.applied_at is None


def test_application_status_transition(db):
    job = _make_job(db, "a2")
    app = Application(job_id=job.id)
    db.add(app)
    db.flush()

    app.status = ApplicationStatus.applied
    app.applied_at = datetime.now(timezone.utc)
    db.flush()

    fetched = db.query(Application).filter_by(id=app.id).first()
    assert fetched.status == ApplicationStatus.applied
    assert fetched.applied_at is not None


def test_create_application_document(db):
    job = _make_job(db, "a3")
    app = Application(job_id=job.id)
    db.add(app)
    db.flush()

    doc = ApplicationDocument(
        application_id=app.id,
        doc_type=DocType.resume,
        version=1,
        path="/storage/resumes/resume_acme_swe_20260611_v1.pdf",
        is_current=True,
    )
    db.add(doc)
    db.flush()

    assert doc.id is not None
    assert doc.generation_feedback is None


def test_document_version_history(db):
    job = _make_job(db, "a4")
    app = Application(job_id=job.id)
    db.add(app)
    db.flush()

    v1 = ApplicationDocument(
        application_id=app.id,
        doc_type=DocType.resume,
        version=1,
        path="/storage/resumes/v1.pdf",
        is_current=False,
    )
    v2 = ApplicationDocument(
        application_id=app.id,
        doc_type=DocType.resume,
        version=2,
        path="/storage/resumes/v2.pdf",
        generation_feedback="Too formal",
        is_current=True,
    )
    db.add_all([v1, v2])
    db.flush()

    docs = (
        db.query(ApplicationDocument)
        .filter_by(application_id=app.id, doc_type=DocType.resume)
        .order_by(ApplicationDocument.version)
        .all()
    )
    assert len(docs) == 2
    assert docs[0].is_current is False
    assert docs[1].generation_feedback == "Too formal"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker compose run --rm web pytest tests/test_models_application.py -v
```
Expected: `ImportError: cannot import name 'Application'`

- [ ] **Step 3: Write `app/models/application.py`**

```python
import uuid
import enum
from datetime import datetime

from sqlalchemy import String, Boolean, Text, DateTime, Integer, Enum as SAEnum, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ApplicationStatus(enum.Enum):
    not_applied = "not_applied"
    applied = "applied"
    interviewing = "interviewing"
    offered = "offered"
    rejected = "rejected"
    withdrawn = "withdrawn"


class DocType(enum.Enum):
    resume = "resume"
    cover_letter = "cover_letter"


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id"), nullable=False
    )
    status: Mapped[ApplicationStatus] = mapped_column(
        SAEnum(ApplicationStatus), default=ApplicationStatus.not_applied, nullable=False
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    outreach_contacts: Mapped[list] = mapped_column(JSONB, default=list)

    job = relationship("Job", backref="application")
    documents: Mapped[list["ApplicationDocument"]] = relationship(
        "ApplicationDocument", back_populates="application"
    )


class ApplicationDocument(Base):
    __tablename__ = "application_documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("applications.id"), nullable=False
    )
    doc_type: Mapped[DocType] = mapped_column(SAEnum(DocType), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    path: Mapped[str] = mapped_column(String, nullable=False)
    generation_feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    application: Mapped["Application"] = relationship(
        "Application", back_populates="documents"
    )
```

- [ ] **Step 4: Update `app/models/__init__.py`**

```python
from app.models.job import Job, JobStatus
from app.models.profile import Profile
from app.models.application import Application, ApplicationDocument, ApplicationStatus, DocType
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
docker compose run --rm web pytest tests/test_models_application.py -v
```
Expected: `4 passed`

- [ ] **Step 6: Commit**

```bash
git add app/models/application.py app/models/__init__.py tests/test_models_application.py
git commit -m "feat: Application, ApplicationDocument models with status and version history"
```

---

## Task 8: Alembic Migrations

**Files:**
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/versions/0001_initial.py` (auto-generated)

- [ ] **Step 1: Initialize Alembic**

```bash
docker compose run --rm web alembic init alembic
```
Expected: creates `alembic.ini` and `alembic/` directory.

- [ ] **Step 2: Update `alembic/env.py`** — replace the generated file with this:

```python
from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool
from alembic import context

from app.config import settings
from app.database import Base
import app.models  # noqa: F401 — registers all models with Base.metadata

config = context.config
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

- [ ] **Step 3: Generate initial migration**

```bash
docker compose run --rm web alembic revision --autogenerate -m "initial"
```
Expected: creates `alembic/versions/<hash>_initial.py` with tables for `jobs`, `profiles`, `applications`, `application_documents`.

- [ ] **Step 4: Verify migration file looks correct**

Open the generated file. Confirm it contains `op.create_table` calls for all 4 tables. If any table is missing, check that `app/models/__init__.py` imports all models.

- [ ] **Step 5: Run migration**

```bash
docker compose run --rm web alembic upgrade head
```
Expected:
```
INFO  [alembic.runtime.migration] Running upgrade  -> <hash>, initial
```

- [ ] **Step 6: Verify tables exist**

```bash
docker compose exec postgres psql -U jobapp -d jobapp -c "\dt"
```
Expected: lists `jobs`, `profiles`, `applications`, `application_documents`, `alembic_version`.

- [ ] **Step 7: Commit**

```bash
git add alembic.ini alembic/
git commit -m "feat: alembic migrations with initial schema"
```

---

## Task 9: FastAPI App Skeleton + Health Endpoint

**Files:**
- Create: `app/main.py`
- Create: `tests/test_health.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_health.py
def test_health_check(client):
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "db" in data
    assert data["db"] == "ok"


def test_root_redirects_to_apps(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code in (302, 307)
    assert "/apps" in response.headers["location"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker compose run --rm web pytest tests/test_health.py -v
```
Expected: `ImportError` or connection error

- [ ] **Step 3: Write `app/main.py`**

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database import get_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="JobApp", lifespan=lifespan)

templates = Jinja2Templates(directory="app/templates")


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

- [ ] **Step 4: Create templates directory (needed for future views)**

```bash
mkdir -p app/templates
touch app/templates/.gitkeep
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
docker compose run --rm web pytest tests/test_health.py -v
```
Expected: `2 passed`

- [ ] **Step 6: Verify the running server responds**

```bash
curl http://localhost:8000/health
```
Expected: `{"status":"ok","db":"ok"}`

- [ ] **Step 7: Commit**

```bash
git add app/main.py app/templates/ tests/test_health.py
git commit -m "feat: fastapi app skeleton with health endpoint"
```

---

## Task 10: Celery App Setup

**Files:**
- Create: `app/celery_app.py`
- Create: `tests/test_celery.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_celery.py
from app.celery_app import celery_app


def test_celery_app_configured():
    assert celery_app.conf.broker_url is not None
    assert "redis" in celery_app.conf.broker_url
    assert celery_app.conf.result_backend is not None


def test_ping_task():
    from app.celery_app import ping
    result = ping.apply()
    assert result.result == "pong"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker compose run --rm web pytest tests/test_celery.py -v
```
Expected: `ImportError: cannot import name 'celery_app'`

- [ ] **Step 3: Write `app/celery_app.py`**

```python
from celery import Celery
from app.config import settings

celery_app = Celery(
    "jobapp",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=[],  # task modules registered here as plans build them
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

celery_app.conf.beat_schedule = {}


@celery_app.task
def ping():
    return "pong"
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
docker compose run --rm web pytest tests/test_celery.py -v
```
Expected: `2 passed`

- [ ] **Step 5: Verify worker starts cleanly**

```bash
docker compose logs worker
```
Expected: lines containing `celery@<hostname> ready` and no errors.

- [ ] **Step 6: Send a test task to the live worker**

```bash
docker compose run --rm web python -c "
from app.celery_app import ping
result = ping.delay()
print(result.get(timeout=10))
"
```
Expected: `pong`

- [ ] **Step 7: Commit**

```bash
git add app/celery_app.py tests/test_celery.py
git commit -m "feat: celery app with redis broker and ping smoke test"
```

---

## Task 11: Full Test Suite Pass

- [ ] **Step 1: Run all tests**

```bash
docker compose run --rm web pytest tests/ -v --tb=short
```
Expected: all tests pass. Count should be:
- `test_config.py`: 2
- `test_models_job.py`: 3
- `test_models_profile.py`: 2
- `test_models_application.py`: 4
- `test_health.py`: 2
- `test_celery.py`: 2
- **Total: 15 passed**

- [ ] **Step 2: Fix any failures before proceeding**

If any test fails, fix the issue. Do not proceed to Plan 2 with a red test suite.

- [ ] **Step 3: Final commit**

```bash
git add -A
git commit -m "chore: plan 01 foundation complete — all 15 tests passing"
```

---

## What Plan 2 Builds

Plan 2 (Profile & Narrative) adds:
- FastAPI routes for profile CRUD
- Jinja2 UI for editing all profile sections
- Narrative questionnaire tab with LLM-generated questions
- Voice summary generation via NVIDIA NIM

Plan 3 (Job Fetching) can be built in parallel with Plan 2 — it only depends on the `Job` model and Celery app from this plan.
