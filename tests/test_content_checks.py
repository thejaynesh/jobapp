"""
The checks of a generated resume and letter against the profile.
"""

import copy
from types import SimpleNamespace
from unittest.mock import patch

from app.services import content_checks, document_content, pdf_text

PROFILE = {
    "personal": {"name": "Jaynesh Bhandari"},
    "narrative": {"summary": "Backend engineer."},
    "skills": {"languages": ["Python", "Go"], "data": ["Kafka", "PostgreSQL"]},
    "experience": [
        {"id": "exp-1", "company": "Initech", "role": "Engineer", "start_date": "Jan 2022",
         "end_date": "Dec 2023", "tech": ["Python"],
         "bullets": ["Cut ingestion latency by 40% with a Python rewrite",
                     "Ran the on-call rota for 6 services"]},
        {"id": "exp-2", "company": "Hooli", "role": "Intern", "start_date": "Jun 2021",
         "end_date": "Aug 2021", "tech": ["Kafka"], "bullets": ["Built a Kafka consumer"]},
    ],
    "projects": [{"id": "proj-1", "name": "Tracker", "tech": ["Postgres"],
                  "bullets": ["Scores postings every night"]}],
    "education": [{"id": "edu-1", "school": "Northeastern University", "degree": "MS",
                   "start_date": "2024", "end_date": "2026"}],
}

JOB = SimpleNamespace(title="Platform Engineer", company="Globex",
                      description="Kafka at scale for 200 engineers.",
                      required_skills=["Kubernetes"], nice_to_have_skills=[])


def context(**changes):
    """A resume context as build_resume_context makes it from PROFILE."""
    ctx = {
        "narrative_summary": "Backend engineer who ships.",
        "experience": [{**copy.deepcopy(e), "title": e["role"]} for e in PROFILE["experience"]],
        "projects": copy.deepcopy(PROFILE["projects"]),
        "education": copy.deepcopy(PROFILE["education"]),
    }
    ctx.update(changes)
    return ctx


def with_bullets(section, index, bullets, **fields):
    ctx = context()
    ctx[section][index]["bullets"] = bullets
    ctx[section][index].update(fields)
    return ctx


def kinds(findings):
    return [(f["kind"], f["term"]) for f in findings]


class TestTheResume:
    def test_the_profile_as_it_is_has_nothing_to_report(self):
        assert content_checks.check_resume(context(), PROFILE, ["Kafka"], JOB) == []

    def test_a_skill_moved_onto_a_job_that_did_not_use_it(self):
        ctx = with_bullets("experience", 0, ["Cut ingestion latency by 40% with Kafka and Python"])
        findings = content_checks.check_resume(ctx, PROFILE, ["Kafka"], JOB)
        assert kinds(findings) == [("tech", "Kafka")]
        assert "in a bullet under Initech" in findings[0]["detail"]
        assert findings[0]["text"] == "Cut ingestion latency by 40% with Kafka and Python"

    def test_a_skill_the_entry_lists_is_not_reported(self):
        ctx = with_bullets("experience", 1, ["Built a Kafka consumer in Python"])
        findings = content_checks.check_resume(ctx, PROFILE, ["Kafka"], JOB)
        # Kafka is Hooli's; Python is not.
        assert kinds(findings) == [("tech", "Python")]

    def test_a_posting_skill_nowhere_in_the_profile(self):
        ctx = with_bullets("experience", 0, ["Ran the on-call rota for 6 Kubernetes services"])
        assert kinds(content_checks.check_resume(ctx, PROFILE, [], JOB)) == [("tech", "Kubernetes")]

    def test_another_spelling_of_the_same_skill_counts_as_the_entry_having_it(self):
        ctx = with_bullets("projects", 0, ["Scores postings every night in PostgreSQL"])
        assert content_checks.check_resume(ctx, PROFILE, [], JOB) == []

    def test_an_everyday_word_is_not_a_skill(self):
        ctx = with_bullets("experience", 0, ["Helped the team go live with the rewrite"])
        assert content_checks.check_resume(ctx, PROFILE, [], JOB) == []
        ctx = with_bullets("experience", 0, ["Rewrote the ingestion path in Go"])
        assert kinds(content_checks.check_resume(ctx, PROFILE, [], JOB)) == [("tech", "Go")]

    def test_a_figure_from_nowhere(self):
        ctx = with_bullets("projects", 0, ["Scores 3,000 postings every night"])
        assert kinds(content_checks.check_resume(ctx, PROFILE, [], JOB)) == [("figure", "3000")]

    def test_a_changed_title_and_date(self):
        ctx = context()
        ctx["experience"][0].update(title="Senior Engineer", end_date="Present")
        findings = content_checks.check_resume(ctx, PROFILE, [], JOB)
        assert kinds(findings) == [("identity", "title"), ("identity", "end date")]
        assert "your profile says “Engineer”" in findings[0]["detail"]

    def test_a_changed_degree(self):
        ctx = context()
        ctx["education"][0]["degree"] = "PhD"
        assert kinds(content_checks.check_resume(ctx, PROFILE, [], JOB)) == [("identity", "degree")]

    def test_an_employer_the_profile_does_not_have(self):
        ctx = context()
        ctx["experience"].append({"company": "Umbrella", "title": "Lead", "bullets": []})
        findings = content_checks.check_resume(ctx, PROFILE, [], JOB)
        assert kinds(findings) == [("entry", "Umbrella")]

    def test_an_entry_without_an_id_is_found_by_employer_and_title(self):
        ctx = context()
        del ctx["experience"][0]["id"]
        assert content_checks.check_resume(ctx, PROFILE, [], JOB) == []


class TestTheSummary:
    def test_years_beyond_the_dates(self):
        ctx = context(narrative_summary="Backend engineer with 8+ years of experience.")
        findings = content_checks.check_resume(ctx, PROFILE, [], JOB)
        assert kinds(findings) == [("years", "8+ years")]
        assert "add up to 2.1 years" in findings[0]["detail"]

    def test_years_within_rounding_are_left_alone(self):
        ctx = context(narrative_summary="Backend engineer with 2+ years of experience.")
        assert content_checks.check_resume(ctx, PROFILE, [], JOB) == []

    def test_a_skill_and_a_figure_the_profile_never_mentions(self):
        ctx = context(narrative_summary="Engineer who scaled Kubernetes to 90 clusters.")
        assert kinds(content_checks.check_resume(ctx, PROFILE, [], JOB)) == [
            ("tech", "Kubernetes"), ("figure", "90")]

    def test_a_skill_from_anywhere_in_the_profile_is_fine_in_the_summary(self):
        ctx = context(narrative_summary="Python and Kafka engineer; cut latency 40%.")
        assert content_checks.check_resume(ctx, PROFILE, ["Kafka"], JOB) == []


class TestTheLetter:
    def test_the_employers_stack_is_not_a_claim(self):
        body = "Your Kubernetes platform serves 200 engineers, which is why I am writing."
        assert content_checks.check_letter(body, PROFILE, [], JOB) == []

    def test_a_first_person_claim_is(self):
        body = "At Initech I ran Kubernetes for the ingestion team."
        assert kinds(content_checks.check_letter(body, PROFILE, [], JOB)) == [("tech", "Kubernetes")]
        body = "I have run Kafka and Kubernetes clusters."
        assert kinds(content_checks.check_letter(body, PROFILE, [], JOB)) == [("tech", "Kubernetes")]

    def test_wanting_to_work_on_their_stack_is_not_a_claim(self):
        body = "I would love to bring my Kafka work to your Kubernetes platform."
        assert content_checks.check_letter(body, PROFILE, [], JOB) == []

    def test_figures_and_years(self):
        body = "I bring 6 years of backend work. I cut costs by 35%. I ran it for 6 services."
        assert kinds(content_checks.check_letter(body, PROFILE, [], JOB)) == [
            ("years", "6 years"), ("figure", "35")]


class TestAfterAnEdit:
    def test_only_what_the_model_wrote_and_the_edit_kept_is_still_named(self):
        invented = "Cut ingestion latency by 40% with Kafka and Python"
        earlier = content_checks.check_resume(
            with_bullets("experience", 0, [invented, "Wrote 12 services"]), PROFILE, [], JOB)
        assert len(earlier) == 2
        # The edit keeps the first bullet, rewrites the second with the user's
        # own figure, and adds one more.
        now = content_checks.check_resume(
            with_bullets("experience", 0, [invented, "Wrote 14 services", "Mentored 3 interns"]),
            PROFILE, [], JOB)
        assert kinds(content_checks.carried_over(now, earlier)) == [("tech", "Kafka")]

    def test_a_changed_employer_is_named_whoever_changed_it(self):
        ctx = context()
        ctx["experience"][0]["company"] = "Initech Global"
        findings = content_checks.check_resume(ctx, PROFILE, [], JOB)
        assert kinds(content_checks.carried_over(findings, [])) == [("identity", "company")]


class TestWhereTheyAreKept:
    def test_generation_stores_both_documents_findings(self):
        from tests.test_doc_generator import TestGenerateDocuments, _make_app, _mock_db_for_generate

        from app.services.doc_generator import generate_documents

        db = _mock_db_for_generate()
        stack, mocks = TestGenerateDocuments()._patches()
        mocks["summary"].return_value = "Engineer with 10 years of Rust."
        mocks["cover"].return_value = "I shipped 40 features."
        mocks["insights"].return_value = {"keywords": ["Rust"], "requirements": [],
                                          "company_signals": []}
        with stack, patch.object(pdf_text, "extract", return_value="Rust"), \
                patch("app.services.doc_generator.compile_resume_one_page",
                      side_effect=lambda ctx, path: path):
            generate_documents(db, _make_app())
        docs = [call.args[0] for call in db.add.call_args_list]
        resume = next(d for d in docs if d.content["kind"] == "resume")
        letter = next(d for d in docs if d.content["kind"] == "cover_letter")
        assert kinds(resume.content["checks"]) == [("years", "10 years"), ("tech", "Rust")]
        assert kinds(letter.content["checks"]) == [("figure", "40")]
        assert letter.content["keywords"] == ["Rust"]

    def test_an_edit_keeps_the_findings_it_did_not_touch(self, db):
        from tests.test_document_edit import compiled_to, setup

        from app.models.profile import Profile
        from app.services import document_edit

        db.query(Profile).delete()
        db.add(Profile(data=PROFILE))
        db.commit()
        application, docs = setup(db)
        invented = "Cut ingestion latency by 40% with Kafka and Python"
        resume = docs["resume"]
        resume.content = {**document_content.resume(
            with_bullets("experience", 0, [invented, "Ran the on-call rota for 6 services"]),
            {"keywords": []}, PROFILE)}
        resume.content["checks"] = content_checks.check_resume(resume.content["context"], PROFILE)
        db.commit()
        with patch("app.services.doc_generator.compile_resume_one_page",
                   side_effect=compiled_to([])), \
                patch.object(pdf_text, "extract", return_value="text"):
            kept = document_edit.save_resume(db, application, resume, {"summary": "New summary."})
            fixed = document_edit.save_resume(db, application, kept, {
                "experience-0-bullets": "Cut ingestion latency by 40% with a Python rewrite"})
        assert kinds(kept.content["checks"]) == [("tech", "Kafka")]
        assert fixed.content["checks"] == []

    def test_the_page_lists_them(self, client, db):
        from tests.test_document_edit import setup

        application, docs = setup(db)
        docs["resume"].content = {**docs["resume"].content, "checks": [{
            "kind": "tech", "where": "experience", "term": "Kafka", "text": "Ran Kafka",
            "detail": "Kafka is in a bullet under Initech but nowhere in that entry in your profile."}]}
        docs["letter"].content = {**docs["letter"].content, "checks": [{
            "kind": "figure", "where": "letter", "term": "40", "text": "I shipped 40 things.",
            "detail": "40 in the letter is not a figure from your profile."}]}
        db.commit()
        page = client.get(f"/apps/{application.id}").text
        assert "1 thing in this resume your profile does not say" in page
        assert "1 thing in this letter your profile does not say" in page
        assert "nowhere in that entry in your profile" in page and "I shipped 40 things." in page
