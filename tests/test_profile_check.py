"""
Ruling the profile in or out when a generated document comes back empty.

Three things produce an identical blank PDF: an empty profile, tailoring that
returned nothing, and a render that dropped the content. This check answers the
first one without touching the other two — no LLM, no network — so a green
result here is real evidence about where to look next rather than a guess.

The case these tests care most about is the one that is invisible from the
database: entries that exist but carry no bullets. Counting rows says the
profile is healthy; the page shows headings with nothing under them.
"""

import pytest

from app.services.profile_check import readiness, rendered, report

FULL = {
    "personal": {
        "name": "Jane Doe", "email": "jane@example.com", "phone": "+1 555 0100",
        "location": "Boston, MA", "linkedin": "linkedin.com/in/jane",
        "github": "", "website": "",
    },
    "narrative": {"summary": "An engineer who likes hard backend problems."},
    "skills": {"languages": ["Python", "Java"], "tools": ["Docker"]},
    "experience": [{
        "id": "exp-1", "company": "Acme", "role": "Engineer",
        "start_date": "2022", "end_date": "2024",
        "bullets": ["Shipped a thing.", "Shipped another thing."],
        "tech": ["Python"],
    }],
    "projects": [{
        "id": "proj-1", "name": "Widget", "description": "A widget",
        "bullets": ["Built it."], "tech": ["Go"],
    }],
    "education": [{"id": "edu-1", "school": "MIT", "degree": "BS",
                   "field": "CS", "end_date": "2022"}],
}


def without(**overrides):
    return {**FULL, **overrides}


class TestAHealthyProfile:
    def test_it_passes(self):
        assert readiness(FULL)["ok"] is True
        assert readiness(FULL)["blockers"] == []

    def test_the_counts_reflect_what_is_stored(self):
        sections = {s["name"]: s["count"] for s in readiness(FULL)["sections"]}
        assert sections["Experience"] == 1
        assert sections["Projects"] == 1
        assert sections["Skills"] == 3

    def test_every_resume_section_reaches_the_page(self):
        result = rendered(FULL)
        assert result["ok"] is True
        assert all(s["present"] for s in result["sections"])

    def test_the_cover_letter_template_renders_too(self):
        # It shares the header context, so a profile problem usually blanks both.
        assert rendered(FULL)["cover_letter_ok"] is True

    def test_there_is_actual_document_body(self):
        assert rendered(FULL)["characters"] > 200


class TestWhatBlanksADocument:
    def test_no_name_is_called_out(self):
        result = readiness(without(personal={"email": "a@b.c"}))
        assert result["ok"] is False
        assert any("name" in b.lower() for b in result["blockers"])

    def test_nothing_to_say_at_all(self):
        result = readiness(without(experience=[], projects=[]))
        assert result["ok"] is False
        assert any("header" in b.lower() for b in result["blockers"])

    def test_entries_that_exist_but_have_no_bullets(self):
        # The failure that looks like broken code and reads in the database as
        # a healthy profile: three roles, three headings, nothing underneath.
        bulletless = [{**FULL["experience"][0], "bullets": []}]
        result = readiness(without(experience=bulletless))
        assert result["ok"] is False
        assert any("zero bullets" in b for b in result["blockers"])

    def test_blank_strings_do_not_count_as_bullets(self):
        empty = [{**FULL["experience"][0], "bullets": ["", "   "]}]
        assert readiness(without(experience=empty))["experience"][0]["bullets"] == 0

    def test_no_skills_at_all(self):
        result = readiness(without(skills={}))
        assert any("skills" in b.lower() for b in result["blockers"])

    def test_empty_skill_categories_are_not_skills(self):
        result = readiness(without(skills={"languages": [], "tools": []}))
        assert result["ok"] is False

    def test_a_missing_section_is_visibly_missing_in_the_render(self):
        result = rendered(without(projects=[]))
        by_name = {s["name"]: s["present"] for s in result["sections"]}
        assert by_name["Projects"] is False
        assert by_name["Experience"] is True

    def test_an_empty_profile_renders_almost_nothing(self):
        result = rendered({})
        assert result["ok"] is True, "an empty profile is a valid render, not a crash"
        assert not any(s["present"] for s in result["sections"])


class TestWarningsThatAreNotFatal:
    def test_one_bulletless_entry_among_several_warns_rather_than_blocks(self):
        two = [FULL["experience"][0], {"id": "exp-2", "company": "B",
                                       "role": "Dev", "bullets": []}]
        result = readiness(without(experience=two))
        assert result["ok"] is True
        assert any("no bullets" in w for w in result["warnings"])

    def test_missing_education_is_only_a_warning(self):
        result = readiness(without(education=[]))
        assert result["ok"] is True
        assert any("education" in w.lower() for w in result["warnings"])

    def test_missing_ids_are_flagged_because_tailoring_selects_by_them(self):
        # Without ids nothing the model returns can be matched, so curation
        # silently degrades to "the first few in profile order".
        no_id = [{k: v for k, v in FULL["experience"][0].items() if k != "id"}]
        result = readiness(without(experience=no_id))
        assert any("no id" in w for w in result["warnings"])

    def test_a_missing_summary_says_why_it_matters(self):
        result = readiness(without(narrative={}))
        assert any("summary" in w.lower() for w in result["warnings"])


class TestItNeverMakesThingsWorse:
    def test_an_empty_dict_is_handled(self):
        assert readiness({})["ok"] is False

    def test_none_is_handled(self):
        assert readiness(None)["ok"] is False

    def test_a_render_failure_is_reported_not_raised(self, monkeypatch):
        import app.services.profile_check as check

        monkeypatch.setattr(check, "render_latex",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bad template")))
        result = rendered(FULL)
        assert result["ok"] is False
        assert "bad template" in result["error"]

    def test_it_makes_no_llm_call(self, monkeypatch):
        # The whole value of this check is being independent of the thing it is
        # helping to rule out.
        def explode(*args, **kwargs):
            raise AssertionError("the readiness check must not call an LLM")

        monkeypatch.setattr("app.llm.client.chat_completion", explode)
        monkeypatch.setattr("app.llm.providers.call_provider", explode)
        report(FULL)


class TestTheTab:
    def _profile(self, db, data):
        from app.models.profile import Profile

        record = Profile(data=data)
        db.add(record)
        db.commit()
        return record

    def test_it_is_reachable(self, client, db):
        self._profile(db, FULL)
        response = client.get("/profile?tab=check")
        assert response.status_code == 200
        assert "What generation sees" in response.text

    def test_a_healthy_profile_says_to_look_elsewhere(self, client, db):
        self._profile(db, FULL)
        body = client.get("/profile?tab=check").text
        assert "coming from somewhere after this point" in body

    def test_a_blank_profile_says_so_plainly(self, client, db):
        self._profile(db, {"personal": {}, "experience": [], "projects": []})
        body = client.get("/profile?tab=check").text
        assert "nearly empty document" in body

    def test_bulletless_entries_are_visible_per_entry(self, client, db):
        two = [FULL["experience"][0], {"id": "exp-2", "company": "Beta",
                                       "role": "Dev", "bullets": []}]
        self._profile(db, without(experience=two))
        body = client.get("/profile?tab=check").text
        assert "0 bullets" in body

    def test_a_broken_check_does_not_take_the_page_down(self, client, db, monkeypatch):
        # The moment a diagnostic is worth reading is the moment something is
        # wrong, so it must not be the thing that breaks.
        self._profile(db, FULL)
        monkeypatch.setattr(
            "app.services.profile_check.report",
            lambda data: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        response = client.get("/profile?tab=check")
        assert response.status_code == 200
        assert "Could not read the profile" in response.text

    def test_the_other_tabs_still_work(self, client, db):
        self._profile(db, FULL)
        for tab in ("personal", "experience", "skills", "education"):
            assert client.get(f"/profile?tab={tab}").status_code == 200
