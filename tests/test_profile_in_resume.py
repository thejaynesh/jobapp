"""
The "In resumes" switch on profile entries.

Switched off, an experience, project or degree stays in the profile but is left
out of everything written for an employer — the resume, the cover letter,
drafted answers, outreach messages and the education autofill types — until it
is switched back on. Matching still sees it.
"""

from unittest.mock import MagicMock, patch

from app.models.profile import Profile
from app.services.profile_service import for_documents, in_documents

KEPT = {"id": "exp-kept", "company": "Initech", "role": "Engineer",
        "bullets": ["Cut latency by 40%"], "tech": ["Go"]}
HIDDEN = {"id": "exp-hidden", "company": "Hooli", "role": "Intern",
          "bullets": ["Wrote a thing nobody used"], "tech": ["PHP"], "in_resume": False}
PROJECT_HIDDEN = {"id": "proj-hidden", "name": "Old toy", "description": "x",
                  "bullets": ["toy"], "in_resume": False}
EDU_HIDDEN = {"id": "edu-hidden", "school": "Old College", "degree": "AA", "in_resume": False}
EDU_KEPT = {"id": "edu-kept", "school": "Northeastern University", "degree": "MS"}

PROFILE = {
    "personal": {"name": "Jaynesh Bhandari", "email": "j@example.com"},
    "narrative": {"summary": "Backend engineer."},
    "skills": {"languages": ["Python", "Go"]},
    "experience": [HIDDEN, KEPT],
    "projects": [PROJECT_HIDDEN],
    "education": [EDU_HIDDEN, EDU_KEPT],
}


def store(db, data=None):
    db.query(Profile).delete()
    db.add(Profile(data=dict(data or PROFILE)))
    db.commit()


def saved(db):
    db.expire_all()
    return db.query(Profile).first().data


class TestTheFilter:
    def test_an_entry_without_the_flag_is_in(self):
        assert in_documents({"company": "x"})

    def test_a_switched_off_entry_is_left_out_of_every_section(self):
        shown = for_documents(PROFILE)
        assert shown["experience"] == [KEPT]
        assert shown["projects"] == []
        assert shown["education"] == [EDU_KEPT]

    def test_the_profile_itself_is_untouched(self):
        for_documents(PROFILE)
        assert HIDDEN in PROFILE["experience"]


class TestTheSwitch:
    def test_switching_off_and_back_on(self, client, db):
        store(db)
        client.post("/profile/experience/exp-kept/in-resume", data={})
        assert saved(db)["experience"][1]["in_resume"] is False
        client.post("/profile/experience/exp-kept/in-resume", data={"included": "1"})
        assert "in_resume" not in saved(db)["experience"][1]

    def test_the_list_comes_back_showing_it(self, client, db):
        store(db)
        page = client.post("/profile/projects/proj-hidden/in-resume", data={}).text
        assert "left out of resumes" in page
        assert 'hx-post="/profile/projects/proj-hidden/in-resume"' in page

    def test_editing_an_entry_keeps_it_switched_off(self, client, db):
        store(db)
        client.post("/profile/experience/exp-hidden", data={
            "company": "Hooli", "role": "Senior Intern", "bullets": "Wrote a thing",
            "tech": "PHP"})
        entry = saved(db)["experience"][0]
        assert entry["role"] == "Senior Intern" and entry["in_resume"] is False

    def test_an_unknown_section_is_refused(self, client, db):
        store(db)
        assert client.post("/profile/skills/x/in-resume", data={}).status_code == 404

    def test_the_page_renders_the_switch_on_every_entry(self, client, db):
        store(db)
        page = client.get("/profile?tab=experience").text
        assert page.count("In resumes") >= 2


class TestWhatAnEmployerSees:
    def test_the_resume_and_letter_leave_it_out(self):
        from tests.test_doc_generator import TestGenerateDocuments, _make_app

        from app.services.doc_generator import generate_documents

        db = MagicMock()
        db.query.return_value.filter.return_value.count.return_value = 0
        db.query.return_value.filter.return_value.all.return_value = []
        db.query.return_value.first.return_value = MagicMock(data=dict(PROFILE))
        seen = {}

        def select(profile_data, *args, **kwargs):
            seen["selection"] = profile_data
            return {"experience": profile_data["experience"],
                    "projects": profile_data["projects"],
                    "skills": profile_data["skills"]}

        def compile_resume(ctx, path):
            seen["resume"] = ctx
            return path

        stack, mocks = TestGenerateDocuments()._patches()
        with stack, \
                patch("app.services.doc_generator.tailor_resume_selection", side_effect=select), \
                patch("app.services.doc_generator.compile_resume_one_page",
                      side_effect=compile_resume):
            generate_documents(db, _make_app())

        companies = [e.get("company") for e in seen["resume"]["experience"]]
        schools = [e.get("school") for e in seen["resume"]["education"]]
        assert companies == ["Initech"]
        assert "Old College" not in schools and seen["resume"]["projects"] == []
        assert HIDDEN not in seen["selection"]["experience"]
        cover_profile = mocks["cover"].call_args.args[0]
        assert HIDDEN not in cover_profile["experience"]

    def test_a_drafted_answer_leaves_it_out(self, db):
        from app.services import answer_drafts

        store(db)
        with patch("app.services.model_roles.call", return_value="Because.") as model:
            answer_drafts.draft(db, "https://example.com/job", "Why do you want this job?")
        prompt = model.call_args.args[2][1]["content"]
        assert "Initech" in prompt and "Hooli" not in prompt and "Old toy" not in prompt

    def test_an_outreach_message_leaves_it_out(self):
        from app.services.outreach import _evidence_lines

        lines = _evidence_lines(PROFILE)
        assert "Initech" in lines and "Hooli" not in lines and "Old toy" not in lines

    def test_autofill_types_the_first_degree_still_in(self, db):
        from app.routers.agent import _autofill_fields

        store(db)
        assert _autofill_fields(db)["school"] == "Northeastern University"

    def test_matching_still_sees_it(self):
        from app.services.matcher import _build_match_prompt

        job = MagicMock(title="Engineer", company="X", location="Remote", description="Go.",
                        is_remote=True, experience_level="mid", salary_min=None,
                        salary_max=None, required_skills=None, nice_to_have_skills=None,
                        required_years=None, education_required=None, salary_currency=None,
                        salary_period=None, employment_type=None, benefits_note=None,
                        salary_annual_min=None, salary_annual_max=None)
        prompt = " ".join(m["content"] for m in _build_match_prompt(job, PROFILE))
        assert "Hooli" in prompt


class TestTheProfileCheck:
    def test_it_reads_the_profile_as_generation_does_and_names_what_is_out(self):
        from app.services.profile_check import report

        result = report(PROFILE)
        assert result["left_out"] == ["Intern — Hooli", "Old toy", "Old College"]
        assert any("Switched out of resumes" in w for w in result["readiness"]["warnings"])
        experience = next(s for s in result["readiness"]["sections"] if s["name"] == "Experience")
        assert experience["count"] == 1
