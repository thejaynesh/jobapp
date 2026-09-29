"""
The keyword check runs on the text a parser gets out of the PDF.

The real-PDF tests compile the resume template with pdflatex and read it back
with pypdf, the way an ATS would; they skip where LaTeX is not installed.
"""

import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.services import document_content, pdf_text

HAS_LATEX = shutil.which("pdflatex") is not None


def _has_cm_super() -> bool:
    """Whether outline T1 Computer Modern is installed (then the old way was fine)."""
    import subprocess

    found = subprocess.run(["kpsewhich", "sfrm1000.pfb"], capture_output=True, text=True)
    return bool(found.stdout.strip())

CONTEXT = {
    "profile": {"name": "Jaynesh Bhandari",
                "contact": {"email": "j@example.com", "phone": "", "location": "",
                            "linkedin": "", "github": "", "website": ""}},
    "narrative_summary": "Efficient backend engineer who offloads work to the office cluster.",
    "skills": {"Languages": ["Python", "Go"]},
    "skills_ordered": [["Languages", ["Python", "Go"]]],
    "experience": [{"title": "Engineer", "company": "Initech", "start_date": "2022",
                    "end_date": "2024", "bullets": ["Made the ingestion pipeline efficient"]}],
    "education": [],
    "projects": [],
}


class TestReadingTheText:
    def test_line_break_hyphens_are_rejoined(self):
        assert pdf_text.normalize("Kuber-\nnetes and  Go") == "kubernetes and go"

    def test_coverage_is_on_the_normalized_text(self):
        present, missing = pdf_text.coverage("Built on KUBERNETES\nwith Go", ["Kubernetes", "Rust"])
        assert present == ["Kubernetes"] and missing == ["Rust"]

    def test_ligatures_are_counted(self):
        assert pdf_text.ligatures("e\ufb03cient o\ufb00ice") == 2

    def test_an_unreadable_file_is_empty_text(self, tmp_path):
        bad = tmp_path / "x.pdf"
        bad.write_bytes(b"not a pdf")
        assert pdf_text.extract(bad) == ""


@pytest.mark.skipif(not HAS_LATEX, reason="pdflatex is not installed here")
class TestTheCompiledResume:
    def compile(self, tmp_path, tex):
        from app.services.doc_generator import compile_pdf

        return compile_pdf(tex, tmp_path / "resume.pdf")

    def test_the_text_reads_back_as_written(self, tmp_path):
        from app.services.doc_generator import render_latex

        path = self.compile(tmp_path, render_latex("resume.tex.j2", CONTEXT))
        text = pdf_text.extract(path)
        assert pdf_text.ligatures(text) == 0 and pdf_text.garbled(text) == 0
        assert "Experience" in text and "Jaynesh Bhandari" in text
        present, _ = pdf_text.coverage(text, ["efficient", "office", "offloads"])
        assert present == ["efficient", "office", "offloads"]

    def test_the_old_preamble_read_back_garbled(self, tmp_path):
        # The control: the template as every resume was until now — T1 with no
        # outline font, no glyph map. Needs no lmodern-less TeX to show it:
        # dropping the three lines is enough.
        from app.services.doc_generator import render_latex

        tex = render_latex("resume.tex.j2", CONTEXT)
        for line in ("\\IfFileExists{lmodern.sty}{\\usepackage{lmodern}}{}\n",
                     "\\input{glyphtounicode}\n", "\\pdfgentounicode=1\n"):
            assert line in tex
            tex = tex.replace(line, "")
        text = pdf_text.extract(self.compile(tmp_path, tex))
        _, missing = pdf_text.coverage(text, ["efficient", "office", "experience"])
        if shutil.which("kpsewhich") and not _has_cm_super():
            assert missing and pdf_text.garbled(text), text[:200]

    def test_the_letter_is_readable_too(self, tmp_path):
        from app.services.doc_generator import build_cover_letter_context, render_latex

        ctx = build_cover_letter_context(
            {"personal": {"name": "J", "email": "j@example.com"}}, "Globex", "Engineer",
            "I made an office workflow efficient.")
        text = pdf_text.extract(self.compile(tmp_path, render_latex("cover_letter.tex.j2", ctx)))
        assert pdf_text.ligatures(text) == 0 and "efficient" in pdf_text.normalize(text)


class TestTheCheckIsStored:
    def test_coverage_comes_from_the_pdf(self):
        with patch.object(pdf_text, "extract", return_value="Python and Go, no Rust here? no."):
            ats = document_content.ats_check(Path("/x.pdf"), ["Python", "Kubernetes"], CONTEXT)
        assert ats["read_from"] == "pdf" and ats["garbled"] == 0
        assert ats["present"] == ["Python"] and ats["missing"] == ["Kubernetes"]

    def test_garbled_text_is_counted(self):
        with patch.object(pdf_text, "extract", return_value="E\x1ecien t Exp erience"):
            ats = document_content.ats_check(Path("/x.pdf"), ["efficient"], CONTEXT)
        assert ats["garbled"] == 1 and ats["missing"] == ["efficient"]

    def test_an_unreadable_pdf_falls_back_and_says_so(self):
        with patch.object(pdf_text, "extract", return_value=""):
            ats = document_content.ats_check(Path("/x.pdf"), ["Python"], CONTEXT)
        assert ats["read_from"] == "template" and ats["present"] == ["Python"]

    def test_generation_keeps_the_content_and_the_check(self):
        from tests.test_doc_generator import TestGenerateDocuments, _make_app, _mock_db_for_generate

        from app.services.doc_generator import generate_documents

        db = _mock_db_for_generate()
        stack, mocks = TestGenerateDocuments()._patches()
        mocks["insights"].return_value = {"keywords": ["Python", "Terraform"],
                                          "requirements": [], "company_signals": []}
        with stack, patch.object(pdf_text, "extract", return_value="Python everywhere"), \
                patch("app.services.doc_generator.compile_resume_one_page",
                      side_effect=lambda ctx, path: path):
            generate_documents(db, _make_app())
        docs = [call.args[0] for call in db.add.call_args_list]
        resume = next(d for d in docs if d.content["kind"] == "resume")
        letter = next(d for d in docs if d.content["kind"] == "cover_letter")
        assert resume.content["ats"]["present"] == ["Python"]
        assert resume.content["ats"]["missing"] == ["Terraform"]
        assert resume.content["context"]["narrative_summary"] == "Tailored summary."
        assert letter.content["context"]["cover_letter_body"] == "Body."
