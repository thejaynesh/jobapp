"""
Addressing the cover letter to a person the application knows about.
"""

import shutil
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.models.application import ApplicationDocument, DocType
from app.models.outreach import Contact
from app.services import letter_recipient


def person(name=None, role="hiring_manager", source="hunter", archived=False, title="",
           first_name=None, last_name=None):
    return SimpleNamespace(id=uuid.uuid4(), name=name, first_name=first_name, last_name=last_name,
                           role=role, source=source, archived=archived, title=title)


class TestWhoIsChosen:
    def test_a_hiring_manager_before_a_recruiter(self):
        recruiter = person("Rae Recruiter", role="recruiter", source="description")
        manager = person("Ana Lopez", title="Engineering Manager")
        assert letter_recipient.candidates([recruiter, manager]) == [manager, recruiter]

    def test_the_one_the_posting_names_before_one_a_search_found(self):
        found = person("Sam Search", source="hunter")
        named = person("Ana Lopez", source="description")
        added = person("Kim Added", source="manual")
        assert letter_recipient.candidates([found, added, named]) == [named, added, found]

    def test_people_a_letter_is_not_for_are_left_out(self):
        assert letter_recipient.candidates([
            person("Eve Engineer", role="engineer"),
            person("Carl CEO", role="executive"),
            person("Ana Lopez", archived=True),
            person("Recruiting Team", role="recruiter"),
            person("Ana"),
            person(None),
        ]) == []

    def test_a_name_kept_in_parts_counts(self):
        chosen = letter_recipient.candidates([person(first_name="Ana", last_name="Lopez")])
        assert letter_recipient.recipient(chosen[0])["name"] == "Ana Lopez"

    def test_no_one_means_the_fallback(self):
        assert letter_recipient.for_application(SimpleNamespace(contacts=[])) is None
        assert letter_recipient.salutation(None) == "Hiring Manager"


class TestTheLetter:
    def render(self, recipient):
        from app.services.doc_generator import build_cover_letter_context, render_latex

        ctx = build_cover_letter_context({"personal": {"name": "J", "email": "j@example.com"}},
                                         "Globex", "Engineer", "Body.", recipient=recipient)
        return render_latex("cover_letter.tex.j2", ctx)

    def test_it_opens_with_the_name_and_is_addressed_to_them(self):
        tex = self.render({"name": "Ana López", "title": "Engineering Manager & Lead",
                           "contact_id": "x"})
        assert "Dear Ana López," in tex
        assert "Ana López\\\\\nEngineering Manager \\& Lead\\\\\nGlobex" in tex
        assert "Hiring Manager" not in tex

    def test_without_one_it_is_as_before(self):
        tex = self.render(None)
        assert "Dear Hiring Manager," in tex and "Hiring Manager\\\\\nGlobex" in tex

    def test_generation_addresses_it_to_the_best_contact(self):
        from tests.test_doc_generator import TestGenerateDocuments, _make_app, _mock_db_for_generate

        from app.services.doc_generator import generate_documents

        application = _make_app()
        application.contacts = [person("Rae Recruiter", role="recruiter"),
                                person("Ana Lopez", title="Engineering Manager")]
        db = _mock_db_for_generate()
        stack, mocks = TestGenerateDocuments()._patches()
        with stack, patch("app.services.doc_generator.compile_resume_one_page",
                          side_effect=lambda ctx, path: path):
            generate_documents(db, application)
        letter = next(call.args[0] for call in db.add.call_args_list
                      if call.args[0].doc_type == DocType.cover_letter)
        assert letter.content["context"]["recipient"]["name"] == "Ana Lopez"
        rendered = [c.args[1] for c in mocks["render"].call_args_list
                    if c.args[0] == "cover_letter.tex.j2"]
        assert rendered[0]["recipient"]["title"] == "Engineering Manager"


class TestNamesKeepTheirSpelling:
    def test_accented_latin_letters_are_kept_and_the_rest_still_folded(self):
        from app.services.doc_generator import latex_escape

        assert latex_escape("Ana López, Jörg Müller, Antonín Dvořák") == \
            "Ana López, Jörg Müller, Antonín Dvořák"
        # No T1 glyph: folded where NFKD knows a base letter, dropped where not.
        assert latex_escape("\u013fa \u017f \u0126 \u272a") == "La s  "

    @pytest.mark.skipif(shutil.which("pdflatex") is None, reason="pdflatex is not installed here")
    def test_a_letter_to_them_compiles_and_reads_back(self, tmp_path):
        from app.services import pdf_text
        from app.services.doc_generator import (
            build_cover_letter_context, compile_pdf, render_latex)

        ctx = build_cover_letter_context({"personal": {"name": "Zoë Ångström",
                                                        "email": "z@example.com"}}, "Nestlé",
                                         "Engineer", "Body.",
                                         recipient={"name": "José Núñez-Dvořák", "title": ""})
        path = compile_pdf(render_latex("cover_letter.tex.j2", ctx), tmp_path / "l.pdf")
        text = pdf_text.extract(path)
        assert "Dear José Núñez-Dvořák," in text and "Nestlé" in text


class TestChoosingOnThePage:
    def setup(self, db):
        from tests.test_document_edit import setup

        application, docs = setup(db)
        ana = Contact(application_id=application.id, company="Globex", company_key="globex",
                      name="Ana Lopez", title="Engineering Manager", role="hiring_manager",
                      source="description")
        eve = Contact(application_id=application.id, company="Globex", company_key="globex",
                      name="Eve Engineer", role="engineer")
        db.add_all([ana, eve])
        db.commit()
        return application, docs, ana, eve

    def save(self, client, application, doc, recipient):
        from tests.test_document_edit import compiled_to

        with patch("app.services.doc_generator.compile_pdf", side_effect=compiled_to([])):
            return client.post(f"/apps/{application.id}/docs/{doc.id}/edit-letter",
                               data={"body": "Body.", "recipient": recipient})

    def latest(self, db, application):
        from tests.test_document_edit import versions

        return versions(db, application, DocType.cover_letter)[-1].content["context"]["recipient"]

    def test_the_page_offers_who_it_could_go_to(self, client, db):
        application, _, ana, eve = self.setup(db)
        page = client.get(f"/apps/{application.id}").text
        assert "Addressed to" in page and "You have 1 contact it could go to" in page
        assert f'<option value="{ana.id}"' in page and "Ana Lopez — Engineering Manager" in page
        assert str(eve.id) not in page.split('name="recipient"')[1].split("</select>")[0]

    def test_choosing_a_contact_readdresses_the_next_version(self, client, db):
        application, docs, ana, _ = self.setup(db)
        reply = self.save(client, application, docs["letter"], str(ana.id))
        assert reply.headers["HX-Redirect"] == f"/apps/{application.id}"
        assert self.latest(db, application) == {"name": "Ana Lopez", "title": "Engineering Manager",
                                                "contact_id": str(ana.id)}

    def test_keep_and_none(self, client, db):
        application, docs, ana, _ = self.setup(db)
        self.save(client, application, docs["letter"], str(ana.id))
        current = db.query(ApplicationDocument).filter_by(
            application_id=application.id, doc_type=DocType.cover_letter, is_current=True).one()
        self.save(client, application, current, "keep")
        assert self.latest(db, application)["name"] == "Ana Lopez"
        current = db.query(ApplicationDocument).filter_by(
            application_id=application.id, doc_type=DocType.cover_letter, is_current=True).one()
        self.save(client, application, current, "none")
        assert self.latest(db, application) is None

    def test_someone_a_letter_is_not_for_cannot_be_chosen(self, client, db):
        application, docs, _, eve = self.setup(db)
        assert self.save(client, application, docs["letter"], str(eve.id)).status_code == 404
