# Document Generation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate tailored LaTeX resumes and cover letters for matched jobs, compile them to PDF with pdflatex, and track version history with feedback-driven regeneration.

**Architecture:** `app/services/doc_generator.py` builds LaTeX from Jinja2 templates and calls pdflatex in a subprocess. `ApplicationDocument` rows in `application_documents` store version history; only one document per (application, doc_type) is `is_current=True`. A Celery task `generate_docs` triggers after matching. An API endpoint `/api/jobs/{job_id}/generate-docs` allows manual regeneration with optional feedback.

**Tech Stack:** Python subprocess + pdflatex (installed in Docker), Jinja2 templates, SQLAlchemy, FastAPI, Celery, NVIDIA NIM (OpenAI-compatible via openai SDK)

---

## File Map

| File | Action | Purpose |
|------|--------|---------|
| `app/services/doc_generator.py` | Create | Core generation logic |
| `app/templates/latex/resume.tex.j2` | Create | LaTeX resume template |
| `app/templates/latex/cover_letter.tex.j2` | Create | LaTeX cover letter template |
| `app/tasks/generate.py` | Create | Celery task wrapper |
| `app/routers/docs.py` | Create | `/api/jobs/{job_id}/generate-docs` endpoint |
| `app/main.py` | Modify | Register docs router |
| `app/celery_app.py` | Modify | Add `app.tasks.generate` to include list |
| `app/tasks/match.py` | Modify | Call `generate_docs.delay(job_id)` for each matched job |
| `tests/test_doc_generator.py` | Create | ~55 tests |

---

### Task 1: LaTeX Jinja2 templates

**Files:**
- Create: `app/templates/latex/resume.tex.j2`
- Create: `app/templates/latex/cover_letter.tex.j2`

- [ ] **Step 1: Create the resume template**

```
app/templates/latex/resume.tex.j2
```

```latex
\documentclass[11pt,letterpaper]{article}
\usepackage[margin=0.75in]{geometry}
\usepackage{hyperref}
\usepackage{enumitem}
\usepackage{titlesec}
\usepackage[T1]{fontenc}
\pagestyle{empty}

\titleformat{\section}{\large\bfseries}{}{0em}{}[\titlerule]
\setlist[itemize]{noitemsep, topsep=2pt}

\begin{document}

% Header
\begin{center}
  {\LARGE \textbf{ {{- profile.name | latex_escape -}} }}\\[4pt]
  {{- profile.contact.email | latex_escape }} \quad | \quad
  {{- profile.contact.phone | latex_escape }} \quad | \quad
  {{- profile.contact.location | latex_escape }}
  {% if profile.contact.linkedin %} \quad | \quad \href{ {{- profile.contact.linkedin -}} }{LinkedIn}{% endif %}
  {% if profile.contact.github %} \quad | \quad \href{ {{- profile.contact.github -}} }{GitHub}{% endif %}
\end{center}

% Summary
\section{Summary}
{{ narrative_summary | latex_escape }}

% Skills
\section{Skills}
\begin{itemize}
{% for category, items in skills.items() %}
  \item \textbf{ {{- category | title | latex_escape -}} :} {{ items | join(", ") | latex_escape }}
{% endfor %}
\end{itemize}

% Experience
\section{Experience}
{% for exp in experience %}
\textbf{ {{- exp.title | latex_escape -}} } \hfill {{ exp.start_date | latex_escape }} -- {{ exp.end_date | default("Present") | latex_escape }}\\
\textit{ {{- exp.company | latex_escape -}} } \hfill {{ exp.location | default("") | latex_escape }}
\begin{itemize}
{% for bullet in exp.bullets %}
  \item {{ bullet | latex_escape }}
{% endfor %}
\end{itemize}
{% endfor %}

% Education
\section{Education}
{% for edu in education %}
\textbf{ {{- edu.degree | latex_escape -}} } \hfill {{ edu.graduation_year | latex_escape }}\\
\textit{ {{- edu.school | latex_escape -}} }
{% endfor %}

% Projects (optional)
{% if projects %}
\section{Projects}
{% for proj in projects %}
\textbf{ {{- proj.name | latex_escape -}} } {% if proj.url %}(\href{ {{- proj.url -}} }{link}){% endif %}\\
{{ proj.description | latex_escape }}
\begin{itemize}
{% for bullet in proj.bullets | default([]) %}
  \item {{ bullet | latex_escape }}
{% endfor %}
\end{itemize}
{% endfor %}
{% endif %}

\end{document}
```

- [ ] **Step 2: Create the cover letter template**

```
app/templates/latex/cover_letter.tex.j2
```

```latex
\documentclass[11pt,letterpaper]{article}
\usepackage[margin=1in]{geometry}
\usepackage[T1]{fontenc}
\pagestyle{empty}

\begin{document}

\begin{flushright}
{{ profile.name | latex_escape }}\\
{{ profile.contact.email | latex_escape }}\\
{{ profile.contact.phone | latex_escape }}
\end{flushright}

\vspace{1em}

\begin{flushleft}
Hiring Manager\\
{{ job_company | latex_escape }}
\end{flushleft}

\vspace{1em}

Dear Hiring Manager,

{{ cover_letter_body | latex_escape }}

\vspace{1em}

Sincerely,\\[2em]
{{ profile.name | latex_escape }}

\end{document}
```

- [ ] **Step 3: Verify templates exist**

```bash
ls app/templates/latex/
```
Expected: `cover_letter.tex.j2  resume.tex.j2`

---

### Task 2: Core doc generator — LaTeX escaping, context building, pdflatex

**Files:**
- Create: `app/services/doc_generator.py`
- Test: `tests/test_doc_generator.py` (Task 2 section)

- [ ] **Step 1: Write failing tests for latex_escape and build_resume_context**

```python
# tests/test_doc_generator.py

import pytest
from unittest.mock import MagicMock, patch
import uuid
from datetime import datetime, timezone

# ---- latex_escape tests ----

class TestLatexEscape:
    def test_escapes_ampersand(self):
        from app.services.doc_generator import latex_escape
        assert latex_escape("a & b") == r"a \& b"

    def test_escapes_percent(self):
        from app.services.doc_generator import latex_escape
        assert latex_escape("50% done") == r"50\% done"

    def test_escapes_hash(self):
        from app.services.doc_generator import latex_escape
        assert latex_escape("C#") == r"C\#"

    def test_escapes_dollar(self):
        from app.services.doc_generator import latex_escape
        assert latex_escape("$100") == r"\$100"

    def test_escapes_underscore(self):
        from app.services.doc_generator import latex_escape
        assert latex_escape("snake_case") == r"snake\_case"

    def test_escapes_braces(self):
        from app.services.doc_generator import latex_escape
        assert latex_escape("{a}") == r"\{a\}"

    def test_escapes_caret(self):
        from app.services.doc_generator import latex_escape
        assert latex_escape("x^2") == r"x\^{}2"

    def test_escapes_tilde(self):
        from app.services.doc_generator import latex_escape
        assert latex_escape("hello~world") == r"hello\textasciitilde{}world"

    def test_escapes_backslash(self):
        from app.services.doc_generator import latex_escape
        assert latex_escape(r"a\b") == r"a\textbackslash{}b"

    def test_plain_text_unchanged(self):
        from app.services.doc_generator import latex_escape
        assert latex_escape("Jane Doe") == "Jane Doe"
```

Run: `docker compose run --rm web pytest tests/test_doc_generator.py::TestLatexEscape -v`
Expected: FAIL (module not found)

- [ ] **Step 2: Implement `latex_escape` and Jinja2 environment in `doc_generator.py`**

```python
# app/services/doc_generator.py
import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from app.config import settings

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).parent.parent / "templates" / "latex"
_OUTPUT_DIR = Path(settings.DOCS_OUTPUT_DIR)


def latex_escape(text: str) -> str:
    """Escape special LaTeX characters in user-supplied text."""
    # Order matters: backslash first (it would re-escape others)
    replacements = [
        ("\\", r"\textbackslash{}"),
        ("&", r"\&"),
        ("%", r"\%"),
        ("#", r"\#"),
        ("$", r"\$"),
        ("_", r"\_"),
        ("{", r"\{"),
        ("}", r"\}"),
        ("^", r"\^{}"),
        ("~", r"\textasciitilde{}"),
    ]
    for char, escaped in replacements:
        text = text.replace(char, escaped)
    return text


def _make_jinja_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=False,
    )
    env.filters["latex_escape"] = latex_escape
    return env
```

Run: `docker compose run --rm web pytest tests/test_doc_generator.py::TestLatexEscape -v`
Expected: PASS

- [ ] **Step 3: Write failing tests for `build_resume_context` and `build_cover_letter_context`**

```python
class TestBuildResumeContext:
    def _profile(self):
        return {
            "name": "Jane Doe",
            "contact": {"email": "jane@example.com", "phone": "555-1234", "location": "NY"},
            "skills": {"languages": ["Python", "Go"]},
            "experience": [{"title": "SWE", "company": "Acme", "start_date": "2021-01", "bullets": ["Built API"]}],
            "education": [{"degree": "B.S. CS", "school": "MIT", "graduation_year": "2020"}],
            "narrative": {"summary": "Experienced engineer."},
            "projects": [],
        }

    def test_includes_profile(self):
        from app.services.doc_generator import build_resume_context
        ctx = build_resume_context(self._profile(), tailored_bullets=None)
        assert ctx["profile"]["name"] == "Jane Doe"

    def test_uses_tailored_bullets_when_provided(self):
        from app.services.doc_generator import build_resume_context
        tailored = [{"company": "Acme", "title": "SWE", "bullets": ["Led migration"]}]
        ctx = build_resume_context(self._profile(), tailored_bullets=tailored)
        assert ctx["experience"][0]["bullets"] == ["Led migration"]

    def test_keeps_original_bullets_when_no_tailored(self):
        from app.services.doc_generator import build_resume_context
        ctx = build_resume_context(self._profile(), tailored_bullets=None)
        assert ctx["experience"][0]["bullets"] == ["Built API"]


class TestBuildCoverLetterContext:
    def _profile(self):
        return {
            "name": "Jane Doe",
            "contact": {"email": "jane@example.com", "phone": "555-1234", "location": "NY"},
        }

    def test_includes_job_info(self):
        from app.services.doc_generator import build_cover_letter_context
        ctx = build_cover_letter_context(self._profile(), "Acme Corp", "Backend Engineer", "Great body.")
        assert ctx["job_company"] == "Acme Corp"
        assert ctx["job_title"] == "Backend Engineer"
        assert ctx["cover_letter_body"] == "Great body."

    def test_includes_profile(self):
        from app.services.doc_generator import build_cover_letter_context
        ctx = build_cover_letter_context(self._profile(), "Acme Corp", "Backend Engineer", "Body.")
        assert ctx["profile"]["name"] == "Jane Doe"
```

Run: `docker compose run --rm web pytest tests/test_doc_generator.py::TestBuildResumeContext tests/test_doc_generator.py::TestBuildCoverLetterContext -v`
Expected: FAIL

- [ ] **Step 4: Implement `build_resume_context` and `build_cover_letter_context`**

Add to `app/services/doc_generator.py`:

```python
def build_resume_context(profile_data: dict, tailored_bullets: list[dict] | None) -> dict:
    experience = [dict(exp) for exp in profile_data.get("experience", [])]
    if tailored_bullets:
        bullet_map = {(e["company"], e["title"]): e["bullets"] for e in tailored_bullets}
        for exp in experience:
            key = (exp.get("company", ""), exp.get("title", ""))
            if key in bullet_map:
                exp["bullets"] = bullet_map[key]
    return {
        "profile": profile_data,
        "narrative_summary": profile_data.get("narrative", {}).get("summary", ""),
        "skills": profile_data.get("skills", {}),
        "experience": experience,
        "education": profile_data.get("education", []),
        "projects": profile_data.get("projects", []),
    }


def build_cover_letter_context(profile_data: dict, job_company: str, job_title: str, body: str) -> dict:
    return {
        "profile": profile_data,
        "job_company": job_company,
        "job_title": job_title,
        "cover_letter_body": body,
    }
```

Run: `docker compose run --rm web pytest tests/test_doc_generator.py::TestBuildResumeContext tests/test_doc_generator.py::TestBuildCoverLetterContext -v`
Expected: PASS

- [ ] **Step 5: Write failing tests for `render_latex` and `compile_pdf`**

```python
class TestRenderLatex:
    def test_renders_resume_template(self, tmp_path):
        from app.services.doc_generator import render_latex
        ctx = {
            "profile": {
                "name": "Jane Doe",
                "contact": {"email": "j@j.com", "phone": "555", "location": "NY", "linkedin": "", "github": ""},
            },
            "narrative_summary": "Engineer.",
            "skills": {"languages": ["Python"]},
            "experience": [],
            "education": [],
            "projects": [],
        }
        tex = render_latex("resume.tex.j2", ctx)
        assert "Jane Doe" in tex
        assert r"\documentclass" in tex

    def test_renders_cover_letter_template(self):
        from app.services.doc_generator import render_latex
        ctx = {
            "profile": {"name": "Jane Doe", "contact": {"email": "j@j.com", "phone": "555"}},
            "job_company": "Acme",
            "job_title": "SWE",
            "cover_letter_body": "I am excited to apply.",
        }
        tex = render_latex("cover_letter.tex.j2", ctx)
        assert "Acme" in tex
        assert "I am excited to apply." in tex

    def test_escapes_special_chars(self):
        from app.services.doc_generator import render_latex
        ctx = {
            "profile": {
                "name": "Jane & Doe",
                "contact": {"email": "j@j.com", "phone": "555", "location": "NY", "linkedin": "", "github": ""},
            },
            "narrative_summary": "Engineer with 50% success.",
            "skills": {},
            "experience": [],
            "education": [],
            "projects": [],
        }
        tex = render_latex("resume.tex.j2", ctx)
        assert r"\&" in tex
        assert r"50\%" in tex


class TestCompilePdf:
    def test_calls_pdflatex(self, tmp_path):
        from app.services.doc_generator import compile_pdf
        tex_source = r"\documentclass{article}\begin{document}Hello\end{document}"
        with patch("app.services.doc_generator.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            out_path = compile_pdf(tex_source, tmp_path / "out.pdf")
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "pdflatex" in cmd

    def test_raises_on_nonzero_exit(self, tmp_path):
        from app.services.doc_generator import compile_pdf, DocGenerationError
        with patch("app.services.doc_generator.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Fatal error")
            with pytest.raises(DocGenerationError, match="pdflatex"):
                compile_pdf(r"\bad", tmp_path / "out.pdf")
```

Run: `docker compose run --rm web pytest tests/test_doc_generator.py::TestRenderLatex tests/test_doc_generator.py::TestCompilePdf -v`
Expected: FAIL

- [ ] **Step 6: Implement `render_latex` and `compile_pdf`**

Add to `app/services/doc_generator.py`:

```python
class DocGenerationError(Exception):
    pass


def render_latex(template_name: str, context: dict) -> str:
    env = _make_jinja_env()
    template = env.get_template(template_name)
    return template.render(**context)


def compile_pdf(tex_source: str, output_path: Path) -> Path:
    with tempfile.TemporaryDirectory() as tmpdir:
        tex_file = Path(tmpdir) / "document.tex"
        tex_file.write_text(tex_source, encoding="utf-8")
        result = subprocess.run(
            ["pdflatex", "-interaction=nonstopmode", "-output-directory", tmpdir, str(tex_file)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise DocGenerationError(f"pdflatex failed (exit {result.returncode}): {result.stderr[-500:]}")
        compiled = Path(tmpdir) / "document.pdf"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(str(compiled), str(output_path))
    return output_path
```

Run: `docker compose run --rm web pytest tests/test_doc_generator.py::TestRenderLatex tests/test_doc_generator.py::TestCompilePdf -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add app/services/doc_generator.py app/templates/latex/ tests/test_doc_generator.py
git commit -m "feat: add LaTeX doc generator core — escape, context, render, compile"
```

---

### Task 3: LLM tailoring — cover letter body + tailored bullets

**Files:**
- Modify: `app/services/doc_generator.py`
- Test: `tests/test_doc_generator.py` (Task 3 section)

- [ ] **Step 1: Write failing tests for `generate_cover_letter_body` and `tailor_resume_bullets`**

```python
class TestGenerateCoverLetterBody:
    def _profile(self):
        return {
            "name": "Jane Doe",
            "narrative": {"summary": "Experienced engineer."},
            "skills": {"languages": ["Python", "Go"]},
            "experience": [{"title": "SWE", "company": "Acme", "bullets": ["Built API"]}],
        }

    def test_returns_string(self):
        from app.services.doc_generator import generate_cover_letter_body
        mock_resp = "I am excited to apply to Acme Corp for the Backend Engineer role."
        with patch("app.services.doc_generator.chat_completion", return_value=mock_resp):
            body = generate_cover_letter_body(self._profile(), "Acme Corp", "Backend Engineer",
                                              "Job desc here", "fake-key", "http://fake", "fake-model")
        assert isinstance(body, str)
        assert len(body) > 0

    def test_passes_correct_llm_args(self):
        from app.services.doc_generator import generate_cover_letter_body
        with patch("app.services.doc_generator.chat_completion", return_value="Body text.") as mock_cc:
            generate_cover_letter_body(self._profile(), "Acme", "SWE", "Desc",
                                       "my-key", "http://base", "my-model")
        _, kwargs = mock_cc.call_args
        assert kwargs["api_key"] == "my-key"
        assert kwargs["base_url"] == "http://base"
        assert kwargs["model"] == "my-model"

    def test_returns_fallback_on_llm_error(self):
        from app.services.doc_generator import generate_cover_letter_body
        with patch("app.services.doc_generator.chat_completion", side_effect=Exception("timeout")):
            body = generate_cover_letter_body(self._profile(), "Acme", "SWE", "Desc",
                                              "key", "url", "model")
        assert isinstance(body, str)
        assert len(body) > 0


class TestTailorResumeBullets:
    def _profile(self):
        return {
            "name": "Jane Doe",
            "experience": [
                {"title": "SWE", "company": "Acme", "bullets": ["Built API", "Led team"]},
                {"title": "Intern", "company": "Beta", "bullets": ["Wrote tests"]},
            ],
        }

    def test_returns_list_of_dicts(self):
        from app.services.doc_generator import tailor_resume_bullets
        raw = '[{"company": "Acme", "title": "SWE", "bullets": ["Led platform migration"]}]'
        with patch("app.services.doc_generator.chat_completion", return_value=raw):
            result = tailor_resume_bullets(self._profile(), "Backend Engineer", "Job desc",
                                           "key", "url", "model")
        assert isinstance(result, list)
        assert result[0]["company"] == "Acme"

    def test_strips_markdown_code_block(self):
        from app.services.doc_generator import tailor_resume_bullets
        raw = '```json\n[{"company": "Acme", "title": "SWE", "bullets": ["Led migration"]}]\n```'
        with patch("app.services.doc_generator.chat_completion", return_value=raw):
            result = tailor_resume_bullets(self._profile(), "Backend Engineer", "Desc",
                                           "key", "url", "model")
        assert result[0]["bullets"] == ["Led migration"]

    def test_returns_empty_list_on_parse_failure(self):
        from app.services.doc_generator import tailor_resume_bullets
        with patch("app.services.doc_generator.chat_completion", return_value="not json"):
            result = tailor_resume_bullets(self._profile(), "SWE", "Desc", "key", "url", "model")
        assert result == []

    def test_returns_empty_list_on_llm_error(self):
        from app.services.doc_generator import tailor_resume_bullets
        with patch("app.services.doc_generator.chat_completion", side_effect=Exception("fail")):
            result = tailor_resume_bullets(self._profile(), "SWE", "Desc", "key", "url", "model")
        assert result == []
```

Run: `docker compose run --rm web pytest tests/test_doc_generator.py::TestGenerateCoverLetterBody tests/test_doc_generator.py::TestTailorResumeBullets -v`
Expected: FAIL

- [ ] **Step 2: Implement `generate_cover_letter_body` and `tailor_resume_bullets`**

Add to `app/services/doc_generator.py`. First import the shared `chat_completion` from matcher:

```python
from app.services.matcher import chat_completion
```

Then add:

```python
def generate_cover_letter_body(
    profile_data: dict,
    job_company: str,
    job_title: str,
    job_description: str,
    api_key: str,
    base_url: str,
    model: str,
) -> str:
    skills = profile_data.get("skills", {})
    skills_flat = [s for cat in skills.values() for s in cat]
    summary = profile_data.get("narrative", {}).get("summary", "")
    name = profile_data.get("name", "Candidate")
    experience = profile_data.get("experience", [])
    exp_summary = "; ".join(f"{e.get('title')} at {e.get('company')}" for e in experience[:3])

    messages = [
        {
            "role": "system",
            "content": (
                "You write professional, concise cover letter bodies (3 paragraphs, ~200 words). "
                "Write in first person. No salutation or sign-off — body text only. "
                "Be specific about the candidate's relevant skills and experience."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Candidate: {name}\nSummary: {summary}\n"
                f"Skills: {', '.join(skills_flat)}\nExperience: {exp_summary}\n\n"
                f"Job: {job_title} at {job_company}\n"
                f"Description:\n{job_description[:1500]}"
            ),
        },
    ]
    try:
        return chat_completion(messages=messages, api_key=api_key, base_url=base_url, model=model)
    except Exception as exc:
        logger.error("generate_cover_letter_body LLM error: %s", exc)
        return (
            f"I am excited to apply for the {job_title} position at {job_company}. "
            f"My background in {', '.join(skills_flat[:3])} aligns well with your requirements. "
            "I look forward to discussing how I can contribute to your team."
        )


def _parse_bullets_response(content: str) -> list[dict]:
    import json, re
    text = content.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text.strip())


def tailor_resume_bullets(
    profile_data: dict,
    job_title: str,
    job_description: str,
    api_key: str,
    base_url: str,
    model: str,
) -> list[dict]:
    experience = profile_data.get("experience", [])
    exp_json = [
        {"company": e.get("company"), "title": e.get("title"), "bullets": e.get("bullets", [])}
        for e in experience
    ]
    import json
    messages = [
        {
            "role": "system",
            "content": (
                "You are a resume writer. Given a candidate's experience entries and a job description, "
                "rewrite the bullet points to highlight the most relevant accomplishments. "
                "Return a JSON array with the SAME structure: "
                "[{\"company\": str, \"title\": str, \"bullets\": [str, ...]}]. "
                "Keep bullet count the same. Return ONLY the JSON array."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Job title: {job_title}\nDescription:\n{job_description[:1500]}\n\n"
                f"Experience entries:\n{json.dumps(exp_json, indent=2)}"
            ),
        },
    ]
    try:
        raw = chat_completion(messages=messages, api_key=api_key, base_url=base_url, model=model)
        return _parse_bullets_response(raw)
    except Exception as exc:
        logger.error("tailor_resume_bullets error: %s", exc)
        return []
```

Run: `docker compose run --rm web pytest tests/test_doc_generator.py::TestGenerateCoverLetterBody tests/test_doc_generator.py::TestTailorResumeBullets -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add app/services/doc_generator.py tests/test_doc_generator.py
git commit -m "feat: add LLM cover letter body and tailored resume bullet generation"
```

---

### Task 4: Top-level `generate_documents` orchestrator + DB version management

**Files:**
- Modify: `app/services/doc_generator.py`
- Test: `tests/test_doc_generator.py` (Task 4 section)

- [ ] **Step 1: Write failing tests for `_next_version`, `_set_only_current`, and `generate_documents`**

```python
_APP_ID = uuid.uuid4()
_JOB_ID = uuid.uuid4()


def _make_app(job_id=None):
    app = MagicMock()
    app.id = _APP_ID
    app.job_id = job_id or _JOB_ID
    app.job = MagicMock()
    app.job.title = "Backend Engineer"
    app.job.company = "GoodCorp"
    app.job.description = "Python FastAPI Docker."
    app.job.status = None
    return app


class TestNextVersion:
    def test_returns_one_when_no_docs(self):
        from app.services.doc_generator import _next_version
        from app.models.application import DocType
        db = MagicMock()
        db.query.return_value.filter.return_value.count.return_value = 0
        assert _next_version(db, _APP_ID, DocType.resume) == 1

    def test_increments_existing_count(self):
        from app.services.doc_generator import _next_version
        from app.models.application import DocType
        db = MagicMock()
        db.query.return_value.filter.return_value.count.return_value = 3
        assert _next_version(db, _APP_ID, DocType.resume) == 4


class TestSetOnlyCurrent:
    def test_sets_existing_docs_not_current(self):
        from app.services.doc_generator import _set_only_current
        from app.models.application import DocType
        db = MagicMock()
        old_doc = MagicMock()
        old_doc.is_current = True
        db.query.return_value.filter.return_value.all.return_value = [old_doc]
        new_doc = MagicMock()
        _set_only_current(db, _APP_ID, DocType.resume, new_doc)
        assert old_doc.is_current is False
        assert new_doc.is_current is True


class TestGenerateDocuments:
    def test_creates_resume_and_cover_letter_docs(self):
        from app.services.doc_generator import generate_documents
        from app.models.application import DocType
        from app.models.job import JobStatus
        db = MagicMock()
        app = _make_app()
        db.query.return_value.filter.return_value.count.return_value = 0
        db.query.return_value.filter.return_value.all.return_value = []
        db.query.return_value.first.return_value = MagicMock(data={
            "name": "Jane Doe",
            "contact": {"email": "j@j.com", "phone": "555", "location": "NY"},
            "narrative": {"summary": "Engineer."},
            "skills": {"languages": ["Python"]},
            "experience": [],
            "education": [],
            "projects": [],
        })
        with patch("app.services.doc_generator.tailor_resume_bullets", return_value=[]):
            with patch("app.services.doc_generator.generate_cover_letter_body", return_value="Body."):
                with patch("app.services.doc_generator.render_latex", return_value=r"\documentclass{article}\begin{document}ok\end{document}"):
                    with patch("app.services.doc_generator.compile_pdf") as mock_compile:
                        mock_compile.return_value = Path("/fake/path.pdf")
                        generate_documents(db, app)
        assert db.add.call_count == 2

    def test_updates_job_status_to_docs_generated(self):
        from app.services.doc_generator import generate_documents
        from app.models.job import JobStatus
        db = MagicMock()
        app = _make_app()
        db.query.return_value.filter.return_value.count.return_value = 0
        db.query.return_value.filter.return_value.all.return_value = []
        db.query.return_value.first.return_value = MagicMock(data={
            "name": "Jane Doe",
            "contact": {"email": "j@j.com", "phone": "555", "location": "NY"},
            "narrative": {"summary": "Engineer."},
            "skills": {"languages": ["Python"]},
            "experience": [],
            "education": [],
            "projects": [],
        })
        with patch("app.services.doc_generator.tailor_resume_bullets", return_value=[]):
            with patch("app.services.doc_generator.generate_cover_letter_body", return_value="Body."):
                with patch("app.services.doc_generator.render_latex", return_value=r"\documentclass{article}\begin{document}ok\end{document}"):
                    with patch("app.services.doc_generator.compile_pdf") as mock_compile:
                        mock_compile.return_value = Path("/fake/path.pdf")
                        generate_documents(db, app)
        assert app.job.status == JobStatus.docs_generated

    def test_regenerate_uses_feedback(self):
        from app.services.doc_generator import generate_documents
        db = MagicMock()
        app = _make_app()
        db.query.return_value.filter.return_value.count.return_value = 1
        db.query.return_value.filter.return_value.all.return_value = []
        db.query.return_value.first.return_value = MagicMock(data={
            "name": "Jane Doe",
            "contact": {"email": "j@j.com", "phone": "555", "location": "NY"},
            "narrative": {"summary": "Engineer."},
            "skills": {"languages": ["Python"]},
            "experience": [],
            "education": [],
            "projects": [],
        })
        with patch("app.services.doc_generator.tailor_resume_bullets", return_value=[]) as mock_tb:
            with patch("app.services.doc_generator.generate_cover_letter_body", return_value="Body."):
                with patch("app.services.doc_generator.render_latex", return_value=r"\documentclass{article}\begin{document}ok\end{document}"):
                    with patch("app.services.doc_generator.compile_pdf") as mock_compile:
                        mock_compile.return_value = Path("/fake/path.pdf")
                        generate_documents(db, app, feedback="Make bullets more concise")
        # feedback passed into tailoring call
        call_kwargs = mock_tb.call_args
        assert call_kwargs is not None
```

Run: `docker compose run --rm web pytest tests/test_doc_generator.py::TestNextVersion tests/test_doc_generator.py::TestSetOnlyCurrent tests/test_doc_generator.py::TestGenerateDocuments -v`
Expected: FAIL

- [ ] **Step 2: Implement `_next_version`, `_set_only_current`, `generate_documents`**

Add to `app/services/doc_generator.py` (add `import uuid` at top, already there via Path):

```python
from app.models.application import Application, ApplicationDocument, DocType
from app.models.job import Job, JobStatus
from app.models.profile import Profile


def _next_version(db, application_id: uuid.UUID, doc_type: DocType) -> int:
    count = (
        db.query(ApplicationDocument)
        .filter(
            ApplicationDocument.application_id == application_id,
            ApplicationDocument.doc_type == doc_type,
        )
        .count()
    )
    return count + 1


def _set_only_current(db, application_id: uuid.UUID, doc_type: DocType, new_doc: ApplicationDocument) -> None:
    old_docs = (
        db.query(ApplicationDocument)
        .filter(
            ApplicationDocument.application_id == application_id,
            ApplicationDocument.doc_type == doc_type,
        )
        .all()
    )
    for doc in old_docs:
        doc.is_current = False
    new_doc.is_current = True


def generate_documents(db, application: Application, feedback: str | None = None) -> None:
    api_key = settings.NVIDIA_NIM_API_KEY
    base_url = settings.NVIDIA_NIM_BASE_URL
    model = settings.NVIDIA_NIM_MODEL

    profile = db.query(Profile).first()
    profile_data = profile.data if profile else {}
    job = application.job

    tailored_bullets = tailor_resume_bullets(
        profile_data, job.title, job.description or "", api_key, base_url, model
    )
    cover_body = generate_cover_letter_body(
        profile_data, job.company, job.title, job.description or "", api_key, base_url, model
    )

    # Resume
    resume_ctx = build_resume_context(profile_data, tailored_bullets if tailored_bullets else None)
    resume_tex = render_latex("resume.tex.j2", resume_ctx)
    resume_version = _next_version(db, application.id, DocType.resume)
    resume_filename = f"{application.id}_resume_v{resume_version}.pdf"
    resume_path = _OUTPUT_DIR / str(application.id) / resume_filename
    compiled_resume = compile_pdf(resume_tex, resume_path)

    resume_doc = ApplicationDocument(
        application_id=application.id,
        doc_type=DocType.resume,
        version=resume_version,
        path=str(compiled_resume),
        generation_feedback=feedback,
    )
    _set_only_current(db, application.id, DocType.resume, resume_doc)
    db.add(resume_doc)

    # Cover letter
    cl_ctx = build_cover_letter_context(profile_data, job.company, job.title, cover_body)
    cl_tex = render_latex("cover_letter.tex.j2", cl_ctx)
    cl_version = _next_version(db, application.id, DocType.cover_letter)
    cl_filename = f"{application.id}_cover_letter_v{cl_version}.pdf"
    cl_path = _OUTPUT_DIR / str(application.id) / cl_filename
    compiled_cl = compile_pdf(cl_tex, cl_path)

    cl_doc = ApplicationDocument(
        application_id=application.id,
        doc_type=DocType.cover_letter,
        version=cl_version,
        path=str(compiled_cl),
        generation_feedback=feedback,
    )
    _set_only_current(db, application.id, DocType.cover_letter, cl_doc)
    db.add(cl_doc)

    job.status = JobStatus.docs_generated
    db.commit()
```

Run: `docker compose run --rm web pytest tests/test_doc_generator.py::TestNextVersion tests/test_doc_generator.py::TestSetOnlyCurrent tests/test_doc_generator.py::TestGenerateDocuments -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add app/services/doc_generator.py tests/test_doc_generator.py
git commit -m "feat: add generate_documents orchestrator with version tracking"
```

---

### Task 5: Celery task + config update

**Files:**
- Create: `app/tasks/generate.py`
- Modify: `app/celery_app.py`
- Modify: `app/tasks/match.py`
- Test: `tests/test_doc_generator.py` (Task 5 section)

- [ ] **Step 1: Write failing tests for the Celery generate task**

```python
class TestGenerateDocsTask:
    def test_task_calls_generate_documents(self):
        from app.tasks.generate import generate_docs
        mock_db = MagicMock()
        mock_app = MagicMock()
        mock_app.id = _APP_ID
        mock_db.query.return_value.filter.return_value.first.return_value = mock_app
        with patch("app.tasks.generate.generate_documents") as mock_gd:
            with patch("app.tasks.generate.SessionLocal", return_value=mock_db):
                generate_docs(str(_APP_ID))
        mock_gd.assert_called_once_with(mock_db, mock_app, feedback=None)

    def test_task_passes_feedback(self):
        from app.tasks.generate import generate_docs
        mock_db = MagicMock()
        mock_app = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = mock_app
        with patch("app.tasks.generate.generate_documents") as mock_gd:
            with patch("app.tasks.generate.SessionLocal", return_value=mock_db):
                generate_docs(str(_APP_ID), feedback="Add metrics")
        _, kwargs = mock_gd.call_args
        assert kwargs["feedback"] == "Add metrics"

    def test_task_is_celery_task(self):
        from app.tasks.generate import generate_docs
        assert hasattr(generate_docs, "delay")

    def test_task_closes_db_on_error(self):
        from app.tasks.generate import generate_docs
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None
        with patch("app.tasks.generate.generate_documents", side_effect=Exception("DB fail")):
            with patch("app.tasks.generate.SessionLocal", return_value=mock_db):
                generate_docs(str(_APP_ID))
        mock_db.close.assert_called_once()

    def test_generate_task_in_celery_includes(self):
        from app.celery_app import celery_app
        import app.tasks.generate  # noqa
        assert "app.tasks.generate" in celery_app.conf.include
```

Run: `docker compose run --rm web pytest tests/test_doc_generator.py::TestGenerateDocsTask -v`
Expected: FAIL

- [ ] **Step 2: Create `app/tasks/generate.py`**

```python
import logging

from app.celery_app import celery_app
from app.database import SessionLocal
from app.models.application import Application
from app.services.doc_generator import generate_documents

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.generate.generate_docs", bind=False)
def generate_docs(application_id: str, feedback: str | None = None) -> dict:
    db = SessionLocal()
    try:
        import uuid
        app = db.query(Application).filter(Application.id == uuid.UUID(application_id)).first()
        if not app:
            logger.warning("generate_docs: application %s not found", application_id)
            return {"status": "not_found"}
        generate_documents(db, app, feedback=feedback)
        return {"status": "ok", "application_id": application_id}
    except Exception as exc:
        logger.error("generate_docs failed for %s: %s", application_id, exc)
        return {"status": "error", "error": str(exc)}
    finally:
        db.close()
```

- [ ] **Step 3: Update `app/celery_app.py` include list**

Change:
```python
include=["app.tasks.fetch", "app.tasks.match"],
```
To:
```python
include=["app.tasks.fetch", "app.tasks.match", "app.tasks.generate"],
```

- [ ] **Step 4: Update `app/tasks/match.py` to trigger generate_docs for matched jobs**

In `match_all_new_jobs`, after matching, the caller triggers docs. Instead, update `match.py` to call `generate_docs.delay` for each matched job. Add after `match_all_new_jobs` call in `match_jobs`:

```python
# app/tasks/match.py — updated match_jobs task
@celery_app.task(name="app.tasks.match.match_jobs", bind=False)
def match_jobs() -> dict[str, Any]:
    from app.models.job import Job, JobStatus
    from app.models.application import Application
    db = SessionLocal()
    try:
        result = match_all_new_jobs(db)
        # trigger doc generation for all matched jobs that have an application
        matched_jobs = db.query(Job).filter(Job.status == JobStatus.matched).all()
        for job in matched_jobs:
            for app in job.applications:
                from app.tasks.generate import generate_docs
                generate_docs.delay(str(app.id))
        logger.info(
            "match_jobs complete — processed=%d matched=%d filtered_out=%d errors=%d",
            result["processed"], result["matched"], result["filtered_out"], result["errors"],
        )
        return result
    except Exception as exc:
        logger.error("match_jobs task raised unexpectedly: %s", exc)
        return {"processed": 0, "matched": 0, "filtered_out": 0, "errors": 1}
    finally:
        db.close()
```

Run: `docker compose run --rm web pytest tests/test_doc_generator.py::TestGenerateDocsTask -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/tasks/generate.py app/tasks/match.py app/celery_app.py tests/test_doc_generator.py
git commit -m "feat: add Celery generate_docs task, wire into match pipeline"
```

---

### Task 6: `DOCS_OUTPUT_DIR` config + `/api/jobs/{job_id}/generate-docs` endpoint

**Files:**
- Modify: `app/config.py`
- Create: `app/routers/docs.py`
- Modify: `app/main.py`
- Test: `tests/test_doc_generator.py` (Task 6 section)

- [ ] **Step 1: Check config and add `DOCS_OUTPUT_DIR` if missing**

Check `app/config.py`:
```bash
grep DOCS_OUTPUT_DIR app/config.py
```

If not present, add to `Settings` class:
```python
DOCS_OUTPUT_DIR: str = "/app/generated_docs"
```

- [ ] **Step 2: Write failing tests for the docs router**

```python
class TestGenerateDocsEndpoint:
    def test_returns_202_for_valid_matched_job(self):
        from tests.conftest import client  # use existing test client fixture
        # This is an integration test — use the test app client
        pass  # placeholder — tested via route integration below

    def test_queues_task_for_matched_job(self):
        from app.routers.docs import router
        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        job_id = str(uuid.uuid4())
        with patch("app.routers.docs.generate_docs") as mock_task:
            with patch("app.routers.docs.get_db") as mock_db_dep:
                mock_db = MagicMock()
                mock_db_dep.return_value = iter([mock_db])
                mock_job = MagicMock()
                mock_job.status.value = "matched"
                mock_app_obj = MagicMock()
                mock_app_obj.id = uuid.uuid4()
                mock_job.applications = [mock_app_obj]
                mock_db.query.return_value.filter.return_value.first.return_value = mock_job
                response = client.post(f"/api/jobs/{job_id}/generate-docs")
        assert response.status_code == 202

    def test_returns_404_for_missing_job(self):
        from app.routers.docs import router
        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        job_id = str(uuid.uuid4())
        with patch("app.routers.docs.get_db") as mock_db_dep:
            mock_db = MagicMock()
            mock_db_dep.return_value = iter([mock_db])
            mock_db.query.return_value.filter.return_value.first.return_value = None
            response = client.post(f"/api/jobs/{job_id}/generate-docs")
        assert response.status_code == 404

    def test_accepts_feedback_body(self):
        from app.routers.docs import router
        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        job_id = str(uuid.uuid4())
        with patch("app.routers.docs.generate_docs") as mock_task:
            with patch("app.routers.docs.get_db") as mock_db_dep:
                mock_db = MagicMock()
                mock_db_dep.return_value = iter([mock_db])
                mock_job = MagicMock()
                mock_job.status.value = "matched"
                mock_app_obj = MagicMock()
                mock_app_obj.id = uuid.uuid4()
                mock_job.applications = [mock_app_obj]
                mock_db.query.return_value.filter.return_value.first.return_value = mock_job
                response = client.post(
                    f"/api/jobs/{job_id}/generate-docs",
                    json={"feedback": "More concise bullets please"},
                )
        assert response.status_code == 202
        mock_task.delay.assert_called_once()
        _, kwargs = mock_task.delay.call_args
        assert kwargs.get("feedback") == "More concise bullets please"
```

Run: `docker compose run --rm web pytest tests/test_doc_generator.py::TestGenerateDocsEndpoint -v`
Expected: FAIL

- [ ] **Step 3: Create `app/routers/docs.py`**

```python
import uuid
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.job import Job, JobStatus
from app.tasks.generate import generate_docs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/jobs", tags=["docs"])


class GenerateDocsRequest(BaseModel):
    feedback: Optional[str] = None


@router.post("/{job_id}/generate-docs", status_code=202)
def trigger_generate_docs(
    job_id: uuid.UUID,
    body: GenerateDocsRequest = GenerateDocsRequest(),
    db: Session = Depends(get_db),
):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in (JobStatus.matched, JobStatus.docs_generated):
        raise HTTPException(status_code=422, detail="Job must be in matched or docs_generated status")
    for app in job.applications:
        generate_docs.delay(str(app.id), feedback=body.feedback)
    return {"queued": len(job.applications)}
```

- [ ] **Step 4: Register router in `app/main.py`**

Find where other routers are registered (e.g., `app.include_router(profile_router)`). Add:
```python
from app.routers.docs import router as docs_router
app.include_router(docs_router)
```

Run: `docker compose run --rm web pytest tests/test_doc_generator.py::TestGenerateDocsEndpoint -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/routers/docs.py app/main.py app/config.py tests/test_doc_generator.py
git commit -m "feat: add /api/jobs/{job_id}/generate-docs endpoint with feedback support"
```

---

### Task 7: Full test suite run

- [ ] **Step 1: Run all tests**

```bash
docker compose run --rm web pytest --tb=short -q
```

Expected: All passing (no regressions from previous plans).

- [ ] **Step 2: Commit if any fixes needed**

```bash
git add -u
git commit -m "fix: resolve test failures in plan-05 integration"
```

---

## Notes

- `pdflatex` must be installed in the Docker image. Add to `Dockerfile` if missing: `RUN apt-get update && apt-get install -y texlive-latex-base texlive-fonts-recommended`
- `DOCS_OUTPUT_DIR` default is `/app/generated_docs` — add to `docker-compose.yml` volumes if persistent storage is needed
- The `Job.applications` relationship backref is already defined in `application.py` via `relationship("Job", backref="applications")`
- All LLM calls reuse `chat_completion` from `app.services.matcher` — no duplication
