"""
The "AI prompt" tab: what the matcher actually sends, and what's missing.

The profile form shows what you typed, not what survives into the prompt —
fields read under names nothing writes, sections that vanish when blank. This
is the only view that answers "is my profile filled in properly?" directly.
"""

import hashlib
from datetime import datetime, timezone


def _job(db, title="Backend Engineer", description="Python, Go, PostgreSQL."):
    from app.models.job import Job, JobStatus
    url = f"https://ex.com/{title}"
    job = Job(
        source="linkedin", source_urls=[url], title=title, company="Acme",
        location="NYC", is_remote=False, url=url, description=description,
        experience_level="mid", status=JobStatus.new,
        fetched_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        dedupe_hash=hashlib.sha256(url.encode()).hexdigest()[:32],
    )
    db.add(job)
    db.commit()
    return job


def _profile(db, **overrides):
    from app.models.profile import Profile
    data = {
        "personal": {"name": "Test Person"},
        "target_roles": ["Backend Engineer"],
        "skills": {"languages": ["Python"], "frameworks": ["FastAPI"]},
        "experience": [{"role": "Engineer", "company": "Acme",
                        "start_date": "Jan 2020", "end_date": "Jan 2023"}],
        "narrative": {"summary": "Backend engineer.", "answers": []},
    }
    data.update(overrides)
    db.add(Profile(data=data))
    db.commit()
    return data


class TestBuildingThePreview:
    def test_it_returns_the_real_prompt_not_a_paraphrase(self, db):
        """
        A hand-written description of the prompt would drift from the prompt.
        This has to be the same call the matcher makes.
        """
        from app.services.matcher import _build_match_prompt
        from app.services.prompt_preview import build

        _profile(db)
        job = _job(db)
        preview = build(db)
        expected = _build_match_prompt(job, db_profile_data(db))
        assert preview["system"] == expected[0]["content"]
        assert preview["user"] == expected[1]["content"]

    def test_it_previews_against_the_most_recent_job(self, db):
        from app.services.prompt_preview import build
        _profile(db)
        _job(db, title="Old Role")
        newest = _job(db, title="New Role")
        newest.fetched_at = datetime(2026, 8, 5, tzinfo=timezone.utc)
        db.commit()
        assert build(db)["job"]["title"] == "New Role"

    def test_a_specific_job_can_be_chosen(self, db):
        from app.services.prompt_preview import build
        _profile(db)
        wanted = _job(db, title="Wanted")
        _job(db, title="Other")
        assert build(db, str(wanted.id))["job"]["title"] == "Wanted"

    def test_it_falls_back_to_a_sample_before_anything_is_fetched(self, db):
        """The preview has to work on a fresh install, when it's most needed."""
        from app.services.prompt_preview import build
        _profile(db)
        preview = build(db)
        assert preview["job"]["is_sample"] is True
        assert "Backend Engineer" in preview["user"]

    def test_jobs_without_a_description_are_not_used(self, db):
        from app.services.prompt_preview import build
        _profile(db)
        _job(db, title="Empty", description="")
        assert build(db)["job"]["is_sample"] is True

    def test_the_derived_years_are_shown_per_role(self, db):
        from app.services.prompt_preview import build
        _profile(db)
        preview = build(db)
        assert preview["total_years"] == 3.0
        assert preview["per_role"][0]["years"] == 3.0
        assert preview["per_role"][0]["label"] == "Engineer @ Acme"

    def test_an_unparseable_date_is_flagged_rather_than_counted_as_zero(self, db):
        from app.services.prompt_preview import build
        _profile(db, experience=[{"role": "Engineer", "company": "Acme",
                                  "start_date": "ages ago"}])
        preview = build(db)
        assert preview["per_role"][0]["years"] is None
        assert any("don't parse" in gap for gap in preview["gaps"])


class TestNamingTheGaps:
    def test_a_complete_profile_reports_none(self, db):
        from app.services.prompt_preview import build
        _profile(db)
        assert build(db)["gaps"] == []

    def test_missing_skills_are_called_out_with_what_it_costs(self, db):
        from app.services.prompt_preview import build
        _profile(db, skills={"languages": [], "frameworks": []})
        gaps = build(db)["gaps"]
        assert any("40-point" in gap for gap in gaps)

    def test_missing_target_roles_are_called_out(self, db):
        from app.services.prompt_preview import build
        _profile(db, target_roles=[])
        assert any("20-point" in gap for gap in build(db)["gaps"])

    def test_missing_experience_is_called_out(self, db):
        from app.services.prompt_preview import build
        _profile(db, experience=[])
        assert any("25-point" in gap for gap in build(db)["gaps"])

    def test_a_missing_narrative_summary_is_called_out(self, db):
        from app.services.prompt_preview import build
        _profile(db, narrative={"summary": "", "answers": []})
        assert any("narrative" in gap for gap in build(db)["gaps"])

    def test_an_empty_profile_does_not_crash_the_preview(self, db):
        """The state where you most need to be told what's wrong."""
        from app.models.profile import Profile
        from app.services.prompt_preview import build
        db.add(Profile(data={}))
        db.commit()
        preview = build(db)
        assert len(preview["gaps"]) >= 4
        assert preview["system"]


class TestTheTab:
    def test_the_tab_renders_the_assembled_prompt(self, client, db):
        _profile(db)
        body = client.get("/profile?tab=ai prompt").text
        assert "You are a job-match evaluator" in body
        assert "Candidate: Test Person" in body

    def test_the_tab_is_linked_from_the_profile_nav(self, client, db):
        _profile(db)
        assert "tab=ai prompt" in client.get("/profile").text

    def test_gaps_are_surfaced_on_the_tab(self, client, db):
        _profile(db, skills={"languages": []})
        assert "40-point" in client.get("/profile?tab=ai prompt").text

    def test_the_derived_years_are_shown_on_the_tab(self, client, db):
        _profile(db)
        body = client.get("/profile?tab=ai prompt").text
        assert "Overlapping roles count once" in body
        assert "3.0" in body

    def test_the_partial_endpoint_renders_on_its_own(self, client, db):
        _profile(db)
        response = client.get("/profile/prompt-preview")
        assert response.status_code == 200
        assert "You are a job-match evaluator" in response.text

    def test_a_broken_preview_does_not_take_the_page_down(self, client, db):
        from unittest.mock import patch
        _profile(db)
        with patch("app.services.prompt_preview.build",
                   side_effect=RuntimeError("boom")):
            response = client.get("/profile?tab=ai prompt")
        assert response.status_code == 200
        assert "Couldn't build the preview" in response.text


def db_profile_data(db):
    from app.models.profile import Profile
    return db.query(Profile).first().data
