"""
Editing a generated resume or letter by hand, saved as the next version.

Compilation is patched out except in the one test that runs pdflatex, which
skips where LaTeX is not installed.
"""

import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from app.models.application import Application, ApplicationDocument, DocType
from app.models.job import Job, JobStatus
from app.services import document_content, document_edit, pdf_text

HAS_LATEX = shutil.which("pdflatex") is not None

PROFILE = {
    "experience": [{"id": "exp-1", "company": "Initech", "role": "Engineer",
                    "bullets": ["Cut ingestion latency by 40%", "Ran the on-call rota"]}],
    "projects": [{"id": "proj-1", "name": "Tracker", "bullets": ["Scores postings"]}],
}

CONTEXT = {
    "profile": {"name": "Jaynesh Bhandari",
                "contact": {"email": "j@example.com", "phone": "", "location": "",
                            "linkedin": "", "github": "", "website": ""}},
    "narrative_summary": "Backend engineer.",
    "skills": {"Languages": ["Python", "Go"]},
    "skills_ordered": [["Languages", ["Python", "Go"]]],
    "experience": [{"id": "exp-1", "title": "Engineer", "company": "Initech",
                    "start_date": "2022", "end_date": "2024",
                    "bullets": ["Cut ingestion latency by 40% with a Go rewrite",
                                "Ran the on-call rota"]}],
    "education": [],
    "projects": [{"id": "proj-1", "name": "Tracker", "bullets": ["Scores postings"]}],
}

ATS = {"keywords": ["Python", "Kafka"], "present": ["Python"], "missing": ["Kafka"],
       "read_from": "pdf", "ligatures": 0, "garbled": 0, "chars": 100}


def resume_content():
    return document_content.resume(CONTEXT, ATS, PROFILE)


def letter_content():
    from app.services.doc_generator import build_cover_letter_context

    return document_content.cover_letter(build_cover_letter_context(
        {"personal": {"name": "Jaynesh Bhandari", "email": "j@example.com"}},
        "Globex", "Engineer", "Dear team, hello."))


def setup(db, *, resume=True, letter=True, content=True):
    job = Job(source="greenhouse", url=f"https://x/{uuid.uuid4()}", source_urls=[],
              title="Platform Engineer", company="Globex", description="Kafka.",
              status=JobStatus.docs_generated, fetched_at=datetime.now(timezone.utc),
              dedupe_hash=uuid.uuid4().hex)
    db.add(job)
    db.flush()
    application = Application(job_id=job.id)
    db.add(application)
    db.flush()
    docs = {}
    if resume:
        docs["resume"] = ApplicationDocument(
            application_id=application.id, doc_type=DocType.resume, version=1,
            path="/tmp/r1.pdf", is_current=True,
            content=resume_content() if content else None)
    if letter:
        docs["letter"] = ApplicationDocument(
            application_id=application.id, doc_type=DocType.cover_letter, version=1,
            path="/tmp/c1.pdf", is_current=True,
            content=letter_content() if content else None)
    db.add_all(docs.values())
    db.commit()
    return application, docs


def compiled_to(path_holder):
    def compile(ctx_or_tex, path):
        path_holder.append((ctx_or_tex, path))
        return path
    return compile


def versions(db, application, doc_type):
    db.expire_all()
    return sorted(
        (d for d in db.query(ApplicationDocument)
         .filter(ApplicationDocument.application_id == application.id,
                 ApplicationDocument.doc_type == doc_type)),
        key=lambda d: d.version)


class TestApplyingTheForm:
    def test_summary_bullets_and_skills_are_applied(self):
        form = {"summary": "  Backend engineer\nwho ships.  ",
                "experience-0-bullets": "First\n\n  Second  \n",
                "skills": "Languages: Python, Rust\nCloud: AWS"}
        ctx = document_edit.edited_resume_context(resume_content(), form)
        assert ctx["narrative_summary"] == "Backend engineer who ships."
        assert ctx["experience"][0]["bullets"] == ["First", "Second"]
        assert ctx["skills"] == {"Languages": ["Python", "Rust"], "Cloud": ["AWS"]}
        assert [c for c, _ in ctx["skills_ordered"]] == ["Languages", "Cloud"]

    def test_fields_not_on_the_form_are_left_as_they_were(self):
        ctx = document_edit.edited_resume_context(resume_content(), {"summary": "New."})
        assert ctx["experience"] == CONTEXT["experience"]
        assert ctx["projects"] == CONTEXT["projects"]
        assert ctx["skills"] == CONTEXT["skills"]

    def test_the_stored_content_is_not_changed_in_place(self):
        content = resume_content()
        document_edit.edited_resume_context(content, {"experience-0-bullets": "Only this"})
        assert content["context"]["experience"][0]["bullets"] == CONTEXT["experience"][0]["bullets"]

    def test_a_skills_line_without_a_category_still_counts(self):
        ctx = document_edit.edited_resume_context(resume_content(), {"skills": "Python, Go"})
        assert ctx["skills"] == {"Skills": ["Python", "Go"]}

    def test_skills_round_trip_through_the_textarea(self):
        text = document_edit.skills_as_text({"Languages": ["Python", "Go"], "Empty": []})
        assert text == "Languages: Python, Go"
        ctx = document_edit.edited_resume_context(resume_content(), {"skills": text})
        assert ctx["skills"] == {"Languages": ["Python", "Go"]}


class TestTheReview:
    def test_a_rewritten_bullet_is_marked_and_a_copied_one_is_not(self):
        review = document_edit.review(resume_content())
        entry = review["sections"][0]["entries"][0]
        assert entry["field"] == "experience-0-bullets"
        assert entry["heading"] == "Engineer — Initech"
        assert entry["rewritten"] == ["Cut ingestion latency by 40% with a Go rewrite"]
        assert entry["original"] == PROFILE["experience"][0]["bullets"]

    def test_an_entry_the_profile_does_not_know_marks_nothing(self):
        content = resume_content()
        content["original_bullets"] = {}
        entry = document_edit.review(content)["sections"][0]["entries"][0]
        assert entry["original"] is None and entry["rewritten"] == []

    def test_a_letter_or_old_version_has_no_review(self):
        assert document_edit.review(None) is None
        assert document_edit.review(letter_content()) is None


class TestSavingAVersion:
    def test_a_resume_edit_is_the_next_current_version(self, db):
        application, docs = setup(db)
        compiled = []
        with patch("app.services.doc_generator.compile_resume_one_page",
                   side_effect=compiled_to(compiled)), \
                patch.object(pdf_text, "extract", return_value="Python and Kafka"):
            new = document_edit.save_resume(db, application, docs["resume"],
                                            {"experience-0-bullets": "Ran Kafka at scale"})
        old, latest = versions(db, application, DocType.resume)
        assert (old.version, old.is_current) == (1, False)
        assert (latest.version, latest.is_current, latest.id) == (2, True, new.id)
        assert latest.generated_by == document_edit.EDITED_BY
        assert latest.generation_feedback == "Edited by hand from v1"
        assert latest.path.endswith(f"{application.id}_resume_v2.pdf")
        assert compiled[0][0]["experience"][0]["bullets"] == ["Ran Kafka at scale"]
        # The keyword check is re-run on the new PDF with the posting's keywords.
        assert latest.content["ats"]["present"] == ["Python", "Kafka"]
        assert latest.content["ats"]["missing"] == []
        assert latest.content["edited_from"] == 1
        assert latest.content["original_bullets"] == document_content.original_bullets(PROFILE)
        # The earlier version is left as it was.
        assert old.content["context"]["experience"][0]["bullets"] == CONTEXT["experience"][0]["bullets"]

    def test_a_letter_edit_is_the_next_current_version(self, db):
        application, docs = setup(db)
        compiled = []
        with patch("app.services.doc_generator.compile_pdf", side_effect=compiled_to(compiled)):
            document_edit.save_letter(db, application, docs["letter"], "  Dear Ana, I build pipelines.  ")
        old, latest = versions(db, application, DocType.cover_letter)
        assert not old.is_current and latest.is_current and latest.version == 2
        assert latest.content["context"]["cover_letter_body"] == "Dear Ana, I build pipelines."
        assert latest.content["context"]["job_company"] == "Globex"
        assert "Dear Ana, I build pipelines." in compiled[0][0]

    def test_a_version_written_before_content_was_kept_cannot_be_edited(self, db):
        application, docs = setup(db, content=False)
        with pytest.raises(document_edit.NotEditable):
            document_edit.save_resume(db, application, docs["resume"], {"summary": "x"})
        with pytest.raises(document_edit.NotEditable):
            document_edit.save_letter(db, application, docs["letter"], "x")
        assert len(versions(db, application, DocType.resume)) == 1

    @pytest.mark.skipif(not HAS_LATEX, reason="pdflatex is not installed here")
    def test_a_real_edit_compiles_and_reads_back(self, db, tmp_path):
        application, docs = setup(db)
        with patch("app.services.doc_generator._OUTPUT_DIR", tmp_path):
            new = document_edit.save_resume(
                db, application, docs["resume"],
                {"experience-0-bullets": "Ran Kafka clusters for 30 services"})
        assert Path(new.path).exists()
        assert "kafka" in pdf_text.normalize(pdf_text.extract(new.path))
        assert new.content["ats"]["read_from"] == "pdf"
        assert new.content["ats"]["present"] == ["Python", "Kafka"]


class TestTheRoutes:
    def test_saving_a_resume_reloads_the_page(self, client, db):
        application, docs = setup(db)
        with patch("app.services.doc_generator.compile_resume_one_page",
                   side_effect=compiled_to([])), \
                patch.object(pdf_text, "extract", return_value="Python"):
            reply = client.post(f"/apps/{application.id}/docs/{docs['resume'].id}/edit-resume",
                                data={"summary": "Edited summary."})
        assert reply.headers["HX-Redirect"] == f"/apps/{application.id}"
        assert versions(db, application, DocType.resume)[-1].content["context"][
            "narrative_summary"] == "Edited summary."

    def test_saving_a_letter_reloads_the_page(self, client, db):
        application, docs = setup(db)
        with patch("app.services.doc_generator.compile_pdf", side_effect=compiled_to([])):
            reply = client.post(f"/apps/{application.id}/docs/{docs['letter'].id}/edit-letter",
                                data={"body": "New body."})
        assert reply.headers["HX-Redirect"] == f"/apps/{application.id}"

    def test_a_failed_compile_is_said_and_saves_nothing(self, client, db):
        from app.services.doc_generator import DocGenerationError

        application, docs = setup(db)
        with patch("app.services.doc_generator.compile_resume_one_page",
                   side_effect=DocGenerationError("Undefined control sequence <x>")):
            reply = client.post(f"/apps/{application.id}/docs/{docs['resume'].id}/edit-resume",
                                data={"summary": "x"})
        assert "would not compile" in reply.text and "&lt;x&gt;" in reply.text
        assert "HX-Redirect" not in reply.headers
        assert len(versions(db, application, DocType.resume)) == 1

    def test_an_old_version_says_to_regenerate(self, client, db):
        application, docs = setup(db, content=False)
        reply = client.post(f"/apps/{application.id}/docs/{docs['resume'].id}/edit-resume",
                            data={"summary": "x"})
        assert "regenerate once" in reply.text

    def test_a_document_of_another_application_is_not_found(self, client, db):
        application, _ = setup(db)
        _, other = setup(db)
        reply = client.post(f"/apps/{application.id}/docs/{other['resume'].id}/edit-resume",
                            data={"summary": "x"})
        assert reply.status_code == 404


class TestThePage:
    def test_it_shows_the_keyword_check_and_both_editors(self, client, db):
        application, docs = setup(db)
        page = client.get(f"/apps/{application.id}").text
        assert "1 of 2" in page and "the text a parser reads out of this PDF" in page
        assert "Kafka" in page
        assert "Review and edit this resume" in page and "Review and edit this letter" in page
        assert 'name="experience-0-bullets"' in page and "1 rewritten" in page
        assert f"/docs/{docs['resume'].id}/edit-resume" in page
        assert "Dear team, hello." in page

    def test_an_old_version_says_how_to_get_an_editable_one(self, client, db):
        application, _ = setup(db, content=False)
        page = client.get(f"/apps/{application.id}").text
        assert "regenerate once to review and edit it" in page
        assert "Review and edit this letter" not in page
