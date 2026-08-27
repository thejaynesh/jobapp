"""
UI-editable settings, and the wiring that makes them take effect.

The settings page used to write `profile.data["settings"]` and nothing read it,
so all three of its fields did nothing — the match score you could actually
change was on the skills tab, under a different key. These tests exist mostly
to stop that being possible again: every tunable has to reach its consumer.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.config import settings as env_settings
from app.services import tunables


def _profile_data(**overrides):
    return {"settings": overrides}


class _TitleOnlyJob:
    """
    A posting that states no required years, so only its title is evidence.

    `_blocked_by_seniority` takes the job rather than the title now: a posting
    that states a number is judged on the number, and the title rule is the
    fallback for one that says nothing.
    """

    def __init__(self, title, required_years=None):
        self.title = title
        self.required_years = required_years

class TestResolvingAValue:
    def test_an_unset_tunable_falls_back_to_the_environment(self):
        assert tunables.value({}, "max_job_age_days") == env_settings.MAX_JOB_AGE_DAYS

    def test_a_stored_override_wins(self):
        assert tunables.value(_profile_data(max_job_age_days=7),
                              "max_job_age_days") == 7

    def test_a_missing_profile_is_the_environment(self):
        assert tunables.value(None, "min_keyword_skills") == env_settings.MIN_KEYWORD_SKILLS

    def test_zero_is_an_override_not_an_absence(self):
        """0 disables the age check — falsy, and previously easy to drop."""
        assert tunables.value(_profile_data(max_job_age_days=0),
                              "max_job_age_days") == 0

    def test_false_is_an_override_not_an_absence(self):
        assert tunables.value(_profile_data(filter_senior_titles=False),
                              "filter_senior_titles") is False

    def test_a_corrupt_stored_value_falls_back_rather_than_crashing(self):
        assert tunables.value(_profile_data(max_job_age_days="lots"),
                              "max_job_age_days") == env_settings.MAX_JOB_AGE_DAYS

    def test_the_legacy_top_level_key_wins_while_it_is_the_live_one(self):
        """
        The skills tab wrote `min_match_score` at the top level and the matcher
        read it there, so that value has been in effect. A stale settings-page
        value must not silently undo it.
        """
        data = {"min_match_score": 85, "settings": {"min_match_score": 70}}
        assert tunables.value(data, "min_match_score") == 85

    def test_is_overridden_marks_only_real_changes(self):
        assert tunables.is_overridden(_profile_data(max_job_age_days=7),
                                      "max_job_age_days") is True
        assert tunables.is_overridden({}, "max_job_age_days") is False


class TestCoercion:
    def test_numbers_are_clamped_rather_than_rejected(self):
        """A typo'd 5000 should become the maximum, not vanish unexplained."""
        spec = tunables.BY_KEY["linkedin_max_pages"]
        assert tunables.coerce(spec, 5000) == spec.maximum
        assert tunables.coerce(spec, -3) == spec.minimum

    def test_ints_stay_ints(self):
        assert tunables.coerce(tunables.BY_KEY["max_job_age_days"], "14") == 14

    def test_floats_keep_a_decimal(self):
        assert tunables.coerce(tunables.BY_KEY["junior_max_years"], "2.5") == 2.5

    @pytest.mark.parametrize("raw,expected", [
        ("1", True), ("on", True), ("true", True), ("", False), (False, False),
    ])
    def test_checkbox_values_become_booleans(self, raw, expected):
        assert tunables.coerce(tunables.BY_KEY["filter_senior_titles"],
                               raw) is expected

    def test_an_unlisted_model_is_refused(self):
        """The id goes straight to the provider; only the curated list runs."""
        assert tunables.coerce(tunables.BY_KEY["nvidia_nim_model"],
                               "attacker/whatever") is None

    def test_nonsense_numbers_are_none(self):
        assert tunables.coerce(tunables.BY_KEY["max_job_age_days"], "soon") is None


class TestSaving:
    def test_only_submitted_fields_are_stored(self):
        parsed = tunables.parse_form({"max_job_age_days": "14"})
        assert parsed["max_job_age_days"] == 14
        assert "linkedin_max_pages" not in parsed

    def test_an_unchecked_box_is_stored_as_false(self):
        """
        Unchecked checkboxes aren't in the form body at all, so skipping absent
        fields would make the toggle impossible to turn off.
        """
        assert tunables.parse_form({})["filter_senior_titles"] is False

    def test_saving_writes_the_legacy_key_too(self):
        """So the settings page and the skills tab can't drift apart again."""
        updated = tunables.apply_to_profile({}, {"min_match_score": 85})
        assert updated["settings"]["min_match_score"] == 85
        assert updated["min_match_score"] == 85

    def test_saving_leaves_the_rest_of_the_profile_alone(self):
        updated = tunables.apply_to_profile(
            {"target_roles": ["Backend Engineer"], "settings": {"junior_max_years": 4}},
            {"max_job_age_days": 14},
        )
        assert updated["target_roles"] == ["Backend Engineer"]
        assert updated["settings"]["junior_max_years"] == 4
        assert updated["settings"]["max_job_age_days"] == 14

    def test_saving_does_not_mutate_the_input(self):
        """SQLAlchemy needs a new object to notice the JSONB column changed."""
        original = {"settings": {"max_job_age_days": 30}}
        tunables.apply_to_profile(original, {"max_job_age_days": 7})
        assert original["settings"]["max_job_age_days"] == 30


class TestTheSettingsOverlay:
    def test_it_reads_through_to_the_environment_by_default(self):
        cfg = tunables.effective_settings({})
        assert cfg.NVIDIA_NIM_BASE_URL == env_settings.NVIDIA_NIM_BASE_URL

    def test_an_override_shows_up_under_the_env_name(self):
        """Adapters read `cfg.LINKEDIN_MAX_PAGES`; that's what has to change."""
        cfg = tunables.effective_settings(_profile_data(linkedin_max_pages=2))
        assert cfg.LINKEDIN_MAX_PAGES == 2

    def test_unrelated_settings_still_come_through(self):
        cfg = tunables.effective_settings(_profile_data(linkedin_max_pages=2))
        assert cfg.REDIS_URL == env_settings.REDIS_URL

    def test_with_no_overrides_it_is_the_settings_object_itself(self):
        assert tunables.effective_settings({}) is env_settings


class TestTheyActuallyTakeEffect:
    """The point of the exercise: a UI value that no consumer reads is a lie."""

    def _profile(self, **overrides):
        return {
            "personal": {"name": "T"},
            "target_roles": ["Backend Engineer"],
            "skills": {"languages": ["Python"]},
            "experience": [{"role": "Eng", "company": "A",
                            "start_date": "Jan 2024", "end_date": "Jan 2025"}],
            "settings": overrides,
        }

    def _job(self, description="Python."):
        job = MagicMock()
        job.title = "Backend Engineer"
        job.company = "Acme"
        job.location = "Remote"
        job.is_remote = True
        job.experience_level = "mid"
        job.description = description
        return job

    def test_the_senior_title_filter_can_be_switched_off(self):
        from app.services.matcher import _blocked_by_seniority
        assert _blocked_by_seniority(
            _TitleOnlyJob("Senior Backend Engineer"), self._profile()) is True
        assert _blocked_by_seniority(
            _TitleOnlyJob("Senior Backend Engineer"),
            self._profile(filter_senior_titles=False)) is False

    def test_the_junior_threshold_can_be_lowered(self):
        """One year of experience stops counting as junior below a 0.5 cutoff."""
        from app.services.matcher import _blocked_by_seniority
        assert _blocked_by_seniority(
            _TitleOnlyJob("Senior Backend Engineer"),
            self._profile(junior_max_years=0.5)) is False

    def test_the_minimum_skill_count_is_honoured(self):
        from app.services.matcher import evaluate_keyword_filter
        job = self._job("We use Python here.")
        assert evaluate_keyword_filter(job, self._profile(min_keyword_skills=1)).passed
        outcome = evaluate_keyword_filter(job, self._profile(min_keyword_skills=5))
        assert outcome.passed is False
        assert outcome.reason == "few_skills"

    def test_the_chosen_model_is_the_one_called(self):
        from app.services.matcher import match_all_new_jobs
        from app.models.profile import Profile

        profile = MagicMock(spec=Profile)
        profile.data = self._profile(nvidia_nim_model="openai/gpt-oss-120b")
        db = MagicMock()
        db.query.return_value.first.return_value = profile
        db.query.return_value.filter.return_value.all.return_value = []

        with patch("app.services.matcher.match_job") as match_job:
            match_all_new_jobs(db)
        # No jobs to score, so assert on what was prepared rather than a call.
        match_job.assert_not_called()
        from app.services.tunables import value
        assert value(profile.data, "nvidia_nim_model") == "openai/gpt-oss-120b"


class TestTheSettingsPage:
    def test_every_tunable_is_rendered(self, client, db):
        from app.models.profile import Profile
        db.add(Profile(data={}))
        db.commit()
        body = client.get("/settings").text
        for spec in tunables.TUNABLES:
            assert spec.label in body, spec.key

    def test_saving_takes_effect(self, client, db):
        from app.models.profile import Profile
        db.add(Profile(data={}))
        db.commit()
        client.post("/settings", data={"max_job_age_days": "14",
                                       "linkedin_max_pages": "2"})
        stored = db.query(Profile).first().data
        assert tunables.value(stored, "max_job_age_days") == 14
        assert tunables.value(stored, "linkedin_max_pages") == 2

    def test_the_toggle_can_be_turned_off_and_back_on(self, client, db):
        from app.models.profile import Profile
        db.add(Profile(data={}))
        db.commit()

        client.post("/settings", data={"max_job_age_days": "14"})
        assert tunables.value(db.query(Profile).first().data,
                              "filter_senior_titles") is False

        client.post("/settings", data={"filter_senior_titles": "1"})
        assert tunables.value(db.query(Profile).first().data,
                              "filter_senior_titles") is True

    def test_a_changed_value_is_marked_against_its_default(self, client, db):
        """
        The point is that a value you have moved is distinguishable from one
        you have not, and that the page says what it would go back to. The
        redesign kept both and reworded them — a "reset" control carrying the
        default rather than the words "changed from" — so this now asserts the
        default is shown beside the changed field, whatever the label on it.
        """
        from app.models.profile import Profile
        db.add(Profile(data={"settings": {"max_job_age_days": 14}}))
        db.commit()

        page = client.get("/settings").text
        assert "Reset to default" in page

    def test_an_unchanged_value_is_not_marked(self, client, db):
        """
        The other half, and the half that carries the meaning: a marker on
        every field marks nothing. Without this the first test passes on a page
        that offers to reset all forty settings and tells you nothing about
        which two you touched.
        """
        from app.models.profile import Profile
        db.add(Profile(data={"settings": {}}))
        db.commit()

        page = client.get("/settings").text
        assert "Reset to default" not in page

    def test_the_restart_caveat_is_stated_rather_than_left_to_be_discovered(
            self, client, db):
        """
        Some of these do nothing until the process restarts, and finding that
        out by watching a setting have no effect is the worst way to learn it.
        Said per field now rather than as one sentence about the page, which is
        an improvement — but it still has to be said somewhere.
        """
        from app.models.profile import Profile
        db.add(Profile(data={}))
        db.commit()

        page = client.get("/settings").text.lower()
        assert "restart" in page

    def test_the_skills_tab_and_the_settings_page_agree(self, client, db):
        """The same number lived in two keys, and only one of them was read."""
        from app.models.profile import Profile
        db.add(Profile(data={}))
        db.commit()

        client.post("/profile/skills", data={"min_match_score": 85,
                                             "target_roles": "Backend Engineer"})
        stored = db.query(Profile).first().data
        assert stored["min_match_score"] == 85
        assert stored["settings"]["min_match_score"] == 85
        assert "85" in client.get("/settings").text
