import json
import uuid
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

_APP_ID = uuid.uuid4()
_JOB_ID = uuid.uuid4()


def _make_app(job_id=None):
    app = MagicMock()
    app.id = _APP_ID
    app.job_id = job_id or _JOB_ID
    app.job = MagicMock()
    app.job.id = _JOB_ID
    app.job.title = "Backend Engineer"
    app.job.company = "GoodCorp"
    app.job.description = "Python FastAPI Docker."
    app.job.status = None
    return app


# ---------------------------------------------------------------------------
# Task 2 — latex_escape
# ---------------------------------------------------------------------------

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
        assert latex_escape("a\\b") == r"a\textbackslash{}b"

    def test_plain_text_unchanged(self):
        from app.services.doc_generator import latex_escape
        assert latex_escape("Jane Doe") == "Jane Doe"


# ---------------------------------------------------------------------------
# Task 2 — build_resume_context / build_cover_letter_context
# ---------------------------------------------------------------------------

class TestBuildResumeContext:
    def _profile(self):
        return {
            "personal": {"name": "Jane Doe", "email": "jane@example.com", "phone": "555-1234", "location": "NY"},
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
            "personal": {"name": "Jane Doe", "email": "jane@example.com", "phone": "555-1234", "location": "NY"},
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


# ---------------------------------------------------------------------------
# Task 2 — render_latex
# ---------------------------------------------------------------------------

class TestRenderLatex:
    def _base_resume_ctx(self):
        return {
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

    def test_renders_resume_template(self):
        from app.services.doc_generator import render_latex
        tex = render_latex("resume.tex.j2", self._base_resume_ctx())
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
        ctx = self._base_resume_ctx()
        ctx["profile"]["name"] = "Jane & Doe"
        ctx["narrative_summary"] = "Engineer with 50% success."
        tex = render_latex("resume.tex.j2", ctx)
        assert r"\&" in tex
        assert r"50\%" in tex


# ---------------------------------------------------------------------------
# Task 2 — compile_pdf
# ---------------------------------------------------------------------------

class TestCompilePdf:
    def test_calls_pdflatex(self, tmp_path):
        from app.services.doc_generator import compile_pdf
        tex_source = r"\documentclass{article}\begin{document}Hello\end{document}"
        with patch("app.services.doc_generator.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            # also patch shutil.copy since document.pdf won't exist
            with patch("app.services.doc_generator.shutil.copy"):
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


# ---------------------------------------------------------------------------
# Task 3 — LLM cover letter + tailored bullets
# ---------------------------------------------------------------------------

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
            body = generate_cover_letter_body(
                self._profile(), "Acme Corp", "Backend Engineer",
                "Job desc here", "fake-key", "http://fake", "fake-model",
            )
        assert isinstance(body, str)
        assert len(body) > 0

    def test_passes_correct_llm_args(self):
        from app.services.doc_generator import generate_cover_letter_body
        with patch("app.services.doc_generator.chat_completion", return_value="Body text.") as mock_cc:
            generate_cover_letter_body(
                self._profile(), "Acme", "SWE", "Desc",
                "my-key", "http://base", "my-model",
            )
        _, kwargs = mock_cc.call_args
        assert kwargs["api_key"] == "my-key"
        assert kwargs["base_url"] == "http://base"
        assert kwargs["model"] == "my-model"

    def test_returns_fallback_on_llm_error(self):
        from app.services.doc_generator import generate_cover_letter_body
        with patch("app.services.doc_generator.chat_completion", side_effect=Exception("timeout")):
            body = generate_cover_letter_body(
                self._profile(), "Acme", "SWE", "Desc", "key", "url", "model",
            )
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
            result = tailor_resume_bullets(
                self._profile(), "Backend Engineer", "Job desc", "key", "url", "model",
            )
        assert isinstance(result, list)
        assert result[0]["company"] == "Acme"

    def test_strips_markdown_code_block(self):
        from app.services.doc_generator import tailor_resume_bullets
        raw = '```json\n[{"company": "Acme", "title": "SWE", "bullets": ["Led migration"]}]\n```'
        with patch("app.services.doc_generator.chat_completion", return_value=raw):
            result = tailor_resume_bullets(
                self._profile(), "Backend Engineer", "Desc", "key", "url", "model",
            )
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


# ---------------------------------------------------------------------------
# Task 4 — version management + generate_documents
# ---------------------------------------------------------------------------

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


class TestExtractJobInsights:
    def _profile(self):
        return {
            "skills": {"languages": ["Python", "Go"]},
            "experience": [{"company": "Acme", "tech": ["Docker"]}],
            "projects": [{"name": "Proj", "tech": ["FastAPI"]}],
        }

    def test_parses_llm_response(self):
        from app.services.doc_generator import extract_job_insights
        raw = json.dumps({
            "keywords": ["Python", "Kubernetes"],
            "requirements": ["3+ years backend", "API design"],
            "company_signals": ["fintech platform"],
        })
        with patch("app.services.doc_generator.chat_completion", return_value=raw):
            insights = extract_job_insights(
                self._profile(), "SWE", "Acme", "Python Kubernetes APIs", "k", "u", "m",
            )
        assert insights["keywords"] == ["Python", "Kubernetes"]
        assert insights["requirements"] == ["3+ years backend", "API design"]
        assert insights["company_signals"] == ["fintech platform"]

    def test_falls_back_to_profile_terms_on_llm_error(self):
        from app.services.doc_generator import extract_job_insights
        with patch("app.services.doc_generator.chat_completion", side_effect=Exception("down")):
            insights = extract_job_insights(
                self._profile(), "SWE", "Acme",
                "We need Python and Docker experience.", "k", "u", "m",
            )
        assert "Python" in insights["keywords"]
        assert "Docker" in insights["keywords"]
        assert "Go" not in insights["keywords"]

    def test_empty_description_returns_empty_insights(self):
        from app.services.doc_generator import extract_job_insights
        insights = extract_job_insights(self._profile(), "SWE", "Acme", "", "k", "u", "m")
        assert insights == {"keywords": [], "requirements": [], "company_signals": []}


class TestGroundTailoredBullets:
    def _originals(self):
        return [
            {"company": "Acme", "title": "SWE",
             "bullets": ["Cut latency by 20% (500ms to 400ms)", "Led team of 3"]},
        ]

    def test_keeps_bullets_with_original_numbers(self):
        from app.services.doc_generator import _ground_tailored_bullets
        tailored = [{"company": "Acme", "title": "SWE",
                     "bullets": ["Reduced API latency 20%, from 500ms to 400ms", "Led 3-person team"]}]
        result = _ground_tailored_bullets(self._originals(), tailored)
        assert result[0]["bullets"] == tailored[0]["bullets"]

    def test_reverts_bullet_with_fabricated_metric(self):
        from app.services.doc_generator import _ground_tailored_bullets
        tailored = [{"company": "Acme", "title": "SWE",
                     "bullets": ["Cut latency by 90%", "Led team of 3"]}]
        result = _ground_tailored_bullets(self._originals(), tailored)
        assert result[0]["bullets"][0] == "Cut latency by 20% (500ms to 400ms)"
        assert result[0]["bullets"][1] == "Led team of 3"

    def test_drops_invented_employer(self):
        from app.services.doc_generator import _ground_tailored_bullets
        tailored = [{"company": "Google", "title": "SWE", "bullets": ["Did things"]}]
        assert _ground_tailored_bullets(self._originals(), tailored) == []

    def test_handles_non_list_input(self):
        from app.services.doc_generator import _ground_tailored_bullets
        assert _ground_tailored_bullets(self._originals(), {"not": "a list"}) == []


class TestTailorSummary:
    def _profile(self):
        return {"narrative": {"summary": "I build backend systems and ship measurable wins."}}

    def test_returns_llm_summary(self):
        from app.services.doc_generator import tailor_summary
        rewritten = "I build backend systems in Python and ship measurable wins for API-heavy products."
        with patch("app.services.doc_generator.chat_completion", return_value=rewritten):
            out = tailor_summary(self._profile(), "SWE", "Acme", {"keywords": ["Python"]}, "k", "u", "m")
        assert out == rewritten

    def test_falls_back_on_llm_error(self):
        from app.services.doc_generator import tailor_summary
        with patch("app.services.doc_generator.chat_completion", side_effect=Exception("down")):
            out = tailor_summary(self._profile(), "SWE", "Acme", None, "k", "u", "m")
        assert out == self._profile()["narrative"]["summary"]

    def test_rejects_degenerate_output(self):
        from app.services.doc_generator import tailor_summary
        with patch("app.services.doc_generator.chat_completion", return_value="ok"):
            out = tailor_summary(self._profile(), "SWE", "Acme", None, "k", "u", "m")
        assert out == self._profile()["narrative"]["summary"]

    def test_empty_base_summary_skips_llm(self):
        from app.services.doc_generator import tailor_summary
        with patch("app.services.doc_generator.chat_completion") as mock_cc:
            out = tailor_summary({"narrative": {"summary": ""}}, "SWE", "Acme", None, "k", "u", "m")
        assert out == ""
        mock_cc.assert_not_called()


class TestCoverLetterEvidence:
    def test_prompt_includes_evidence_and_feedback(self):
        from app.services.doc_generator import generate_cover_letter_body
        profile = {
            "personal": {"name": "Jane"},
            "narrative": {"summary": "Engineer."},
            "skills": {"languages": ["Python"]},
            "experience": [{"role": "SWE", "company": "Acme", "bullets": ["Cut latency 20%"]}],
            "projects": [{"name": "Proj", "description": "Tool", "bullets": ["Shipped it"]}],
        }
        with patch("app.services.doc_generator.chat_completion", return_value="Body.") as mock_cc:
            generate_cover_letter_body(
                profile, "GoodCorp", "Backend Engineer", "Desc", "k", "u", "m",
                insights={"requirements": ["API design"], "company_signals": ["fintech"]},
                feedback="Shorter please",
            )
        args, kwargs = mock_cc.call_args
        full_text = " ".join(m["content"] for m in kwargs["messages"])
        assert "Cut latency 20%" in full_text
        assert "Shipped it" in full_text
        assert "API design" in full_text
        assert "fintech" in full_text
        assert "Shorter please" in full_text


def _mock_db_for_generate():
    db = MagicMock()
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
    return db


class TestGenerateDocuments:
    def _patches(self):
        from contextlib import ExitStack
        stack = ExitStack()
        mocks = {
            "insights": stack.enter_context(patch(
                "app.services.doc_generator.extract_job_insights",
                return_value={"keywords": [], "requirements": [], "company_signals": []},
            )),
            "bullets": stack.enter_context(patch(
                "app.services.doc_generator.tailor_resume_bullets", return_value=[])),
            "summary": stack.enter_context(patch(
                "app.services.doc_generator.tailor_summary", return_value="Tailored summary.")),
            "cover": stack.enter_context(patch(
                "app.services.doc_generator.generate_cover_letter_body", return_value="Body.")),
            "render": stack.enter_context(patch(
                "app.services.doc_generator.render_latex",
                return_value=r"\documentclass{article}\begin{document}ok\end{document}")),
            "compile": stack.enter_context(patch(
                "app.services.doc_generator.compile_pdf", return_value=Path("/fake/path.pdf"))),
        }
        return stack, mocks

    def test_creates_resume_and_cover_letter_docs(self):
        from app.services.doc_generator import generate_documents
        db = _mock_db_for_generate()
        app = _make_app()
        stack, _ = self._patches()
        with stack:
            generate_documents(db, app)
        assert db.add.call_count == 2

    def test_updates_job_status_to_docs_generated(self):
        from app.services.doc_generator import generate_documents
        from app.models.job import JobStatus
        db = _mock_db_for_generate()
        app = _make_app()
        stack, _ = self._patches()
        with stack:
            generate_documents(db, app)
        assert app.job.status == JobStatus.docs_generated

    def test_regenerate_passes_feedback_to_generators(self):
        from app.services.doc_generator import generate_documents
        db = _mock_db_for_generate()
        app = _make_app()
        stack, mocks = self._patches()
        with stack:
            generate_documents(db, app, feedback="Make bullets more concise")
        for name in ("bullets", "summary", "cover"):
            _, kwargs = mocks[name].call_args
            assert kwargs.get("feedback") == "Make bullets more concise", name

    def test_uses_tailored_summary_in_resume_context(self):
        from app.services.doc_generator import generate_documents
        db = _mock_db_for_generate()
        app = _make_app()
        stack, mocks = self._patches()
        with stack:
            generate_documents(db, app)
        resume_ctx = mocks["render"].call_args_list[0][0][1]
        assert resume_ctx["narrative_summary"] == "Tailored summary."

    def test_commits_after_generation(self):
        from app.services.doc_generator import generate_documents
        db = _mock_db_for_generate()
        app = _make_app()
        stack, _ = self._patches()
        with stack:
            generate_documents(db, app)
        db.commit.assert_called_once()


# ---------------------------------------------------------------------------
# Task 5 — Celery generate_docs task
# ---------------------------------------------------------------------------

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
        assert hasattr(generate_docs, "apply_async")

    def test_task_closes_db_on_error(self):
        from app.tasks.generate import generate_docs
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None
        with patch("app.tasks.generate.generate_documents", side_effect=Exception("DB fail")):
            with patch("app.tasks.generate.SessionLocal", return_value=mock_db):
                result = generate_docs(str(_APP_ID))
        mock_db.close.assert_called_once()

    def test_returns_not_found_when_app_missing(self):
        from app.tasks.generate import generate_docs
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None
        with patch("app.tasks.generate.SessionLocal", return_value=mock_db):
            result = generate_docs(str(_APP_ID))
        assert result["status"] == "not_found"

    def test_generate_task_in_celery_includes(self):
        from app.celery_app import celery_app
        import app.tasks.generate  # noqa
        assert "app.tasks.generate" in celery_app.conf.include


# ---------------------------------------------------------------------------
# Task 6 — /api/jobs/{job_id}/generate-docs endpoint
# ---------------------------------------------------------------------------

class TestGenerateDocsEndpoint:
    def _make_client_with_db(self, mock_db):
        from app.routers.docs import router
        from app.database import get_db
        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        fastapp = FastAPI()
        fastapp.include_router(router)
        fastapp.dependency_overrides[get_db] = lambda: mock_db
        return TestClient(fastapp)

    def _matched_job(self):
        from app.models.job import JobStatus
        mock_job = MagicMock()
        mock_job.status = JobStatus.matched
        mock_app_obj = MagicMock()
        mock_app_obj.id = uuid.uuid4()
        mock_job.applications = [mock_app_obj]
        return mock_job

    def test_returns_202_for_matched_job(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = self._matched_job()
        client = self._make_client_with_db(mock_db)
        job_id = str(uuid.uuid4())
        with patch("app.routers.docs.generate_docs"):
            response = client.post(f"/api/jobs/{job_id}/generate-docs")
        assert response.status_code == 202

    def test_returns_404_for_missing_job(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None
        client = self._make_client_with_db(mock_db)
        job_id = str(uuid.uuid4())
        response = client.post(f"/api/jobs/{job_id}/generate-docs")
        assert response.status_code == 404

    def test_accepts_feedback_body(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = self._matched_job()
        client = self._make_client_with_db(mock_db)
        job_id = str(uuid.uuid4())
        with patch("app.routers.docs.generate_docs") as mock_task:
            response = client.post(
                f"/api/jobs/{job_id}/generate-docs",
                json={"feedback": "More concise bullets please"},
            )
        assert response.status_code == 202
        mock_task.delay.assert_called_once()
        _, kwargs = mock_task.delay.call_args
        assert kwargs.get("feedback") == "More concise bullets please"

    def test_queues_one_task_per_application(self):
        mock_db = MagicMock()
        mock_job = self._matched_job()
        extra_app = MagicMock()
        extra_app.id = uuid.uuid4()
        mock_job.applications.append(extra_app)
        mock_db.query.return_value.filter.return_value.first.return_value = mock_job
        client = self._make_client_with_db(mock_db)
        job_id = str(uuid.uuid4())
        with patch("app.routers.docs.generate_docs") as mock_task:
            response = client.post(f"/api/jobs/{job_id}/generate-docs")
        assert response.status_code == 202
        assert mock_task.delay.call_count == 2
