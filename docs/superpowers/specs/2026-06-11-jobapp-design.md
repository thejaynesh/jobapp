# Job Application Automator — Design Spec
**Date:** 2026-06-11  
**Status:** Approved

---

## Overview

A self-hosted job application automation system running on a Hostinger VPS (8GB RAM, 100GB storage, 2 cores). The system fetches jobs from multiple sources every 5 hours, filters and scores them against a user profile, generates tailored LaTeX resumes and cover letters, tracks applications in a web dashboard, and assists with outreach to potential referrals.

No external storage dependencies (no Google Drive, no Google Sheets). Everything — PDFs, data, UI — lives on the VPS.

---

## Architecture

### Stack
- **Backend:** Python, FastAPI
- **Frontend:** Jinja2 templates + HTMX (no build step, inline updates)
- **Task Queue:** Celery + Redis
- **Database:** PostgreSQL
- **LLM:** NVIDIA NIM API (OpenAI-compatible, free tier)
- **PDF Generation:** pdflatex (texlive-latex-base)
- **Scraping:** Playwright (headless Chromium)

### Component Map
```
FastAPI Web UI (:8000)
    │
    ├── /profile      — profile editor
    ├── /jobs         — matched + rejected job review
    ├── /apps         — application tracker dashboard
    ├── /apps/<id>    — application detail + doc versions + outreach
    └── /settings     — thresholds, schedule, API keys

Redis (broker)
    │
Celery Workers
    ├── fetch_jobs        — runs every 5hr via Celery Beat
    ├── match_jobs        — triggered after fetch
    ├── generate_docs     — triggered per matched job
    ├── outreach_search   — triggered after doc gen
    └── regenerate_doc    — triggered on user feedback

PostgreSQL
    ├── profile (JSON columns)
    ├── jobs
    ├── applications
    └── application_documents

/storage/
    ├── resumes/
    └── cover_letters/
```

### Pipeline Flow
```
Celery Beat (every 5hr)
  → fetch_jobs: scrape all sources, dedupe, save to DB
  → match_jobs: keyword filter → LLM score → mark matched/filtered_out
  → generate_docs (per matched job):
      LLM tailors resume bullets + cover letter
      → pdflatex compiles PDFs
      → save to /storage/
      → create Application row
  → outreach_search (per application):
      find LinkedIn contacts at company
      find emails via Hunter.io
      LLM drafts personalized message
      → save contacts to Application
  → UI reflects new rows immediately
```

---

## Data Models

### Profile
```json
{
  "personal": {
    "name", "email", "phone", "linkedin", "github", "location"
  },
  "experience": [
    {"id", "company", "role", "start_date", "end_date", "bullets": [], "tech": []}
  ],
  "projects": [
    {"id", "name", "description", "tech": [], "bullets": [], "url"}
  ],
  "skills": {
    "languages": [], "frameworks": [], "tools": [], "clouds": []
  },
  "education": [
    {"school", "degree", "start_date", "end_date", "gpa"}
  ],
  "latex_template": "<raw .tex string>",
  "cover_letter_template": "<base text with placeholders>",
  "target_roles": ["Software Engineer", "Backend Engineer"],
  "target_locations": ["Remote", "New York"],
  "excluded_companies": [],
  "min_match_score": 70,
  "narrative": {
    "answers": [
      {"question": "...", "answer": "..."}
    ],
    "summary": "<AI-synthesized paragraph regenerated when answers change>"
  }
}
```

### Job
| Field | Type | Notes |
|---|---|---|
| id | uuid | |
| source | str | adzuna, linkedin, indeed, etc. |
| source_job_id | str | job ID from source if available, null otherwise |
| source_urls | str[] | all URLs where job was found (cross-posted) |
| title | str | |
| company | str | |
| location | str | |
| is_remote | bool | |
| url | str | canonical/first-seen URL |
| description | text | full JD |
| experience_level | str | entry/mid/senior |
| keyword_score | float | |
| llm_score | float | 0–100 |
| llm_reasoning | text | why matched/rejected |
| matched_skills | str[] | |
| missing_skills | str[] | |
| status | enum | new, filtered_out, matched, docs_generated (pipeline states only — set by system) |
| fetched_at | timestamp | |
| dedupe_hash | str | hash(company+title+location) |

### Application
| Field | Type | Notes |
|---|---|---|
| id | uuid | |
| job_id | fk | |
| status | enum | not_applied, applied, interviewing, offered, rejected, withdrawn (user-editable) |
| notes | text | user notes |
| applied_at | timestamp | set when status → applied |
| created_at | timestamp | |
| outreach_contacts | jsonb | [{name, title, linkedin_url, email, draft_message}] |

### ApplicationDocument
| Field | Type | Notes |
|---|---|---|
| id | uuid | |
| application_id | fk | |
| doc_type | enum | resume, cover_letter |
| version | int | increments per regeneration |
| path | str | /storage/resumes/... |
| generation_feedback | text | user input for regeneration, null on v1 |
| is_current | bool | |
| created_at | timestamp | |

---

## Job Fetching

### Sources

**Tier 1 — APIs (no scraping, reliable):**
- Adzuna API — free tier, 250 req/day
- JSearch via RapidAPI — aggregates LinkedIn/Indeed/Glassdoor
- Greenhouse, Lever, Ashby — direct JSON endpoints, no auth

**Tier 2 — Playwright scraping:**
- LinkedIn Jobs (session cookie required)
- Indeed
- Wellfound
- Dice
- Handshake

### Deduplication
Three-layer check — skip insert if any match found:
1. **URL match** — exact URL already in DB (fastest, most reliable)
2. **Source job ID match** — if source provides a job ID (e.g. Adzuna ID, Greenhouse job ID), check that
3. **Content hash** — `dedupe_hash = hash(normalize(company) + normalize(title) + normalize(location))` — catches same job posted across multiple sources

If hash matches but URL differs (cross-posted): keep existing record, append new source URL to a `source_urls[]` field. Update description if JD changed.

---

## Job Matching

### Stage 1 — Keyword Filter (fast, free)
- Job title must fuzzy-match one of `target_roles`
- JD must contain at least 2 skills from user's skill list (configurable in settings, default: 2)
- Company not in `excluded_companies`
- Fails → `status = filtered_out`, record reason, stop

### Stage 2 — LLM Scoring (NVIDIA NIM)
**Prompt input:** profile summary + narrative summary + full JD  
**Prompt output:**
```json
{
  "score": 82,
  "reasoning": "Strong Python/FastAPI match. Missing Kubernetes experience.",
  "matched_skills": ["Python", "FastAPI", "PostgreSQL"],
  "missing_skills": ["Kubernetes"],
  "seniority_fit": true
}
```
- Score < `min_match_score` → `status = filtered_out`
- Score >= threshold → `status = matched`, trigger `generate_docs`

---

## Document Generation

### Resume Tailoring (LLM)
**Input:** full profile + narrative summary + JD + master LaTeX template  
**Output:**
```json
{
  "selected_experience": {"exp_id": ["bullet_id1", "bullet_id2"]},
  "rewritten_bullets": {"exp_id": ["tailored bullet 1", "tailored bullet 2"]},
  "selected_projects": ["project_id1"],
  "skills_to_highlight": ["Python", "FastAPI", "Redis"],
  "summary_line": "Backend engineer with 3 years building distributed systems..."
}
```

### LaTeX Assembly + Compilation
1. Inject tailored content into master `.tex` template
2. `pdflatex -interaction=nonstopmode resume.tex`
3. Save as `resume_<company>_<role>_<YYYYMMDD>_v<n>.pdf` in `/storage/resumes/`

### Cover Letter (LLM)
**Input:** narrative summary + tailored resume content + JD + company name  
**Output:** full cover letter text — personalized, references specific JD points, written in user's voice  
Rendered to PDF via simple cover letter `.tex` template.

### Regeneration with Feedback
- User provides free-text feedback: *"too formal, doesn't mention my leadership at X"*
- Celery task: original prompt + previous output + feedback → new version
- Old version archived (version history kept), new version marked `is_current = true`
- UI shows version list (v1, v2, v3...), all downloadable

---

## Web UI

### `/profile`
Tabs: Personal | Experience | Projects | Skills | Education | Templates | Narrative

**Narrative tab:**
- ~15–20 LLM-generated questions (work style, strengths, collaboration, growth, motivation, unique value)
- Answer per-question, save partial progress
- "Regenerate Summary" button — re-synthesizes all answers into voice paragraph
- Sample questions:
  - *How do colleagues describe your problem-solving style?*
  - *What kinds of problems energize you most?*
  - *How do you handle ambiguity or unclear requirements?*
  - *What do you want employers to know that your resume doesn't show?*

### `/jobs`
Two tabs:

**Matched tab:** jobs with `status = matched` or beyond, linked to application

**Rejected tab:** jobs with `status = filtered_out`
- Shows: keyword_score, llm_score, llm_reasoning, matched_skills, missing_skills
- Actions:
  - **Override** — manually trigger doc generation
  - **Flag as False Negative** — logged for threshold tuning review

Filters: rejection stage (keyword vs LLM), company, date, score range

### `/apps`
Application tracker table:

| Role | Company | Source | Score | Status | Resume | Cover Letter | Outreach | Date |
|---|---|---|---|---|---|---|---|---|
| SWE | Stripe | LinkedIn | 87 | Applied | ⬇ v2 | ⬇ v1 | 3 contacts | Jun 10 |

- **Status** = inline dropdown (HTMX update, no page reload)
  - Options: `Not Applied` → `Applied` → `Interviewing` → `Offered` / `Rejected` / `Withdrawn`
- Resume/Cover Letter = download buttons with version badge
- Outreach = contact count, click to open panel

### `/apps/<id>`
Full application detail:
- Job description preview
- Resume section: current version download + version history + Regenerate button (feedback textarea)
- Cover letter section: same pattern
- Outreach panel: contact cards with name/title/LinkedIn/email + draft message + "Regenerate message" button
- Notes field (free text, auto-saved)

### `/settings`
- `min_match_score` slider (0–100)
- Target roles (add/remove tags)
- Target locations (add/remove tags)
- Excluded companies (add/remove)
- Fetch schedule frequency
- NVIDIA NIM API key
- Hunter.io API key

---

## Outreach Module

**Trigger:** after `generate_docs` completes for an application

**Steps:**
1. **LinkedIn contact search** (Playwright scrape or Proxycurl API):
   - Target: recruiters, engineering managers, SWEs at company
   - Collect: name, title, LinkedIn URL
2. **Email finder:** Hunter.io API (free tier: 25/mo) or public source scraping
3. **LLM message drafting:**
   - Input: contact profile + user narrative + job role + company
   - Output: ~100-word personalized LinkedIn/email message
   - Tone derived from narrative answers

Contacts + messages stored in `Application.outreach_contacts` (jsonb). Editable in UI before copy-paste.

---

## Infrastructure

### VPS Resource Estimate
| Service | RAM |
|---|---|
| FastAPI | ~100MB |
| PostgreSQL | ~150MB |
| Redis | ~50MB |
| Celery workers (4, 2 per core) | ~1GB |
| Playwright (peak, during scrape) | ~400MB |
| pdflatex (peak, during compile) | ~200MB |
| **Total peak** | **~1.4GB** |

8GB RAM — comfortable headroom.

### File Storage
```
/storage/
  resumes/        # resume PDFs
  cover_letters/  # cover letter PDFs
  tex/            # compiled .tex files (debug)
```
Estimate: ~500KB per application × 1000 applications = ~500MB. Well within 100GB.

### Deployment
- Docker Compose: `web`, `worker`, `beat`, `postgres`, `redis`
- Nginx reverse proxy for FastAPI
- `.env` for secrets (API keys, DB credentials)
- Systemd or Docker restart policy for auto-recovery

---

## Error Handling

| Failure | Behavior |
|---|---|
| Scraper blocked / rate limited | Celery retry with exponential backoff (max 3 attempts), mark source as `cooldown` for 1hr |
| LLM API timeout | Retry up to 2x, if fails mark job `match_pending` for next cycle |
| pdflatex compile error | Save error log, mark application `doc_error`, show in UI with error detail |
| Duplicate job | Silently skip on dedupe hash match |
| Outreach search fails | Non-blocking — application still created, outreach shown as empty |

---

## Out of Scope (v1)
- Automated job application submission (apply button clicks)
- Browser extension
- Mobile UI
- Email notifications
- Multi-user support
