"""
Years of experience, derived from the dates the profile form actually collects.

The rubric spends 25 of its 100 points on judging required years against the
candidate's total, and nothing ever collected a years field — so the total was
always zero and that quarter of the score was decided on a blank.
"""

from datetime import date

import pytest

from app.services.experience import (
    entry_span,
    entry_years,
    parse_month,
    total_years,
)


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

class TestParsingTheDatesPeopleActuallyType:
    @pytest.mark.parametrize("text,expected", [
        ("Sep 2024", date(2024, 9, 1)),
        ("September 2024", date(2024, 9, 1)),
        ("sep 2024", date(2024, 9, 1)),
        ("Sep, 2024", date(2024, 9, 1)),
        ("  Sep   2024 ", date(2024, 9, 1)),
        ("2024-09", date(2024, 9, 1)),
        ("2024/9", date(2024, 9, 1)),
        ("09/2024", date(2024, 9, 1)),
        ("9-2024", date(2024, 9, 1)),
    ])
    def test_the_common_forms_all_parse(self, text, expected):
        assert parse_month(text) == expected

    def test_a_bare_year_lands_mid_year(self):
        """
        Otherwise "2022" to "2024" reads as exactly two years, which claims a
        precision the candidate never gave.
        """
        assert parse_month("2022") == date(2022, 7, 1)

    @pytest.mark.parametrize("text", ["Present", "current", "NOW", "ongoing", "to date"])
    def test_an_ongoing_role_ends_today(self, text):
        assert parse_month(text) == date.today()

    def test_a_blank_end_date_means_still_there(self):
        assert parse_month("", default_ongoing=True) == date.today()

    def test_a_blank_start_date_is_not_today(self):
        """A missing start is unknown, not "started this morning"."""
        assert parse_month("") is None

    @pytest.mark.parametrize("text", ["garbage", "13/2024", "2024-13", "last summer",
                                      "1500", None])
    def test_nonsense_is_none_rather_than_guessed(self, text):
        assert parse_month(text) is None


class TestOneRole:
    def test_years_come_from_the_dates(self):
        assert entry_years({"start_date": "Jan 2020", "end_date": "Jan 2023"}) == 3.0

    def test_an_explicit_years_value_wins(self):
        """Profiles that already carry a number keep it."""
        assert entry_years({"years": 4, "start_date": "Jan 2020",
                            "end_date": "Jan 2021"}) == 4.0

    def test_an_unreadable_explicit_value_falls_back_to_the_dates(self):
        assert entry_years({"years": "about four", "start_date": "Jan 2020",
                            "end_date": "Jan 2021"}) == 1.0

    def test_unparseable_dates_give_none_not_zero(self):
        """Zero would be a claim; None lets the caller say "unknown"."""
        assert entry_years({"start_date": "ages ago", "end_date": "recently"}) is None

    def test_an_end_before_the_start_is_rejected(self):
        assert entry_span({"start_date": "Jan 2024", "end_date": "Jan 2020"}) is None

    def test_a_missing_end_date_runs_to_today(self):
        years = entry_years({"start_date": "Jan 2020", "end_date": ""})
        assert years > 5


class TestTheTotal:
    def test_consecutive_roles_add_up(self):
        assert total_years([
            {"start_date": "Jan 2020", "end_date": "Jan 2022"},
            {"start_date": "Jan 2022", "end_date": "Jan 2023"},
        ]) == 3.0

    def test_overlapping_roles_are_counted_once(self):
        """
        Summing them would inflate the total for anyone who held two roles at
        once — and inflating is the direction that hurts, since it pushes the
        candidate past the threshold that keeps senior-titled jobs out.
        """
        assert total_years([
            {"start_date": "Jan 2020", "end_date": "Jan 2024"},
            {"start_date": "Jan 2021", "end_date": "Jan 2022"},
        ]) == 4.0

    def test_partially_overlapping_roles_merge(self):
        assert total_years([
            {"start_date": "Jan 2020", "end_date": "Jan 2022"},
            {"start_date": "Jan 2021", "end_date": "Jan 2023"},
        ]) == 3.0

    def test_a_gap_between_roles_is_not_counted(self):
        assert total_years([
            {"start_date": "Jan 2020", "end_date": "Jan 2021"},
            {"start_date": "Jan 2023", "end_date": "Jan 2024"},
        ]) == 2.0

    def test_entries_without_dates_fall_back_to_explicit_years(self):
        assert total_years([{"years": 2}, {"years": 1.5}]) == 3.5

    def test_no_experience_is_zero(self):
        assert total_years([]) == 0.0
        assert total_years(None) == 0.0

    def test_unreadable_entries_do_not_break_the_total(self):
        assert total_years([
            {"start_date": "Jan 2020", "end_date": "Jan 2022"},
            {"start_date": "whenever"},
        ]) == 2.0


class TestWhatTheMatcherDoesWithIt:
    def _profile(self, experience):
        return {
            "personal": {"name": "Test Person"},
            "target_roles": ["Backend Engineer"],
            "skills": {"languages": ["Python"]},
            "experience": experience,
        }

    def _job(self):
        from unittest.mock import MagicMock
        job = MagicMock()
        job.title = "Backend Engineer"
        job.company = "Acme"
        job.location = "Remote"
        job.is_remote = True
        job.experience_level = "mid"
        job.description = "Python and Go."
        return job

    def test_the_total_reaches_the_prompt(self, ):
        from app.services.matcher import _build_match_prompt
        messages = _build_match_prompt(self._job(), self._profile([
            {"role": "Engineer", "company": "Acme",
             "start_date": "Jan 2020", "end_date": "Jan 2023"},
        ]))
        assert "Total experience: 3 years" in messages[1]["content"]

    def test_each_role_carries_its_own_span(self):
        from app.services.matcher import _build_match_prompt
        messages = _build_match_prompt(self._job(), self._profile([
            {"role": "Engineer", "company": "Acme",
             "start_date": "Jan 2020", "end_date": "Jul 2021"},
        ]))
        assert "Engineer at Acme (1.5 years)" in messages[1]["content"]

    def test_an_unreadable_span_shows_the_dates_rather_than_n_a(self):
        """"N/A years" told the model nothing; the raw dates at least do."""
        from app.services.matcher import _build_match_prompt
        messages = _build_match_prompt(self._job(), self._profile([
            {"role": "Engineer", "company": "Acme", "start_date": "ages ago"},
        ]))
        body = messages[1]["content"]
        assert "ages ago" in body
        assert "N/A years" not in body

    def test_dated_experience_lifts_the_senior_title_prefilter(self):
        """
        This is the bug the derivation fixes: six years of dated history read
        as zero, so senior-titled jobs were dropped before the model saw them.
        """
        from app.services.matcher import _blocked_by_seniority
        profile = self._profile([
            {"role": "Engineer", "company": "Acme",
             "start_date": "Jan 2018", "end_date": "Jan 2024"},
        ])
        assert _blocked_by_seniority(_TitleOnlyJob("Senior Backend Engineer"), profile) is False

    def test_a_genuinely_junior_candidate_is_still_protected(self):
        from app.services.matcher import _blocked_by_seniority
        profile = self._profile([
            {"role": "Intern", "company": "Acme",
             "start_date": "Jun 2024", "end_date": "Sep 2024"},
        ])
        assert _blocked_by_seniority(_TitleOnlyJob("Senior Backend Engineer"), profile) is True
