"""
Proposing target roles the profile supports but the list does not name.

`target_roles` is the narrowest gate in the pipeline and the one nobody
revisits. It is typed once during setup, from whatever titles were on the
user's mind that afternoon, and then quietly decides what the whole system is
allowed to see. A skill picked up since — Flutter, Terraform, a year of Swift —
never becomes a role, so the postings naming it are rejected on the title
before anything reads them.

Two things carry most of these tests.

The suggestion has to be grounded in the profile, because a generic ladder of
engineering titles would suggest the same eight to everybody and mean nothing.

And it has to know what the title gate actually does. That gate passes any
title sharing a single meaningful word with any target role, so a profile
holding "Backend Engineer" already admits every "... Engineer" posting there
is. Offering "Platform Engineer" as an addition would be offering a change that
changes nothing — and a list of those is worse than an empty list, because it
looks like progress.
"""

import json
from unittest.mock import patch

import pytest

from app.models.profile import Profile
from app.services import role_suggest

PROFILE = {
    "target_roles": ["Backend Engineer"],
    "skills": {
        "languages": ["Python", "Dart", "Go"],
        "frameworks": ["Flutter", "FastAPI"],
    },
    "experience": [{"role": "Software Engineer", "company": "Acme"}],
    "projects": [{"name": "A mobile app"}],
}


def _reply(*suggestions):
    return json.dumps({"suggestions": list(suggestions)})


def _suggest(profile=PROFILE, reply=None):
    reply = reply if reply is not None else _reply(
        {"title": "Flutter Developer", "why": "The profile lists Flutter and Dart."},
    )
    with patch("app.services.matcher.chat_completion", return_value=reply):
        return role_suggest.suggest(profile, "k", "https://x/v1", "m")


class TestWhatItProposes:
    def test_it_returns_the_title_and_the_evidence(self):
        outcome = _suggest()
        assert outcome["suggestions"][0]["title"] == "Flutter Developer"
        assert "Flutter" in outcome["suggestions"][0]["why"]

    def test_the_profile_is_what_the_model_is_asked_about(self):
        # Grounded, not generic: the evidence for "Flutter Developer" is that
        # the profile says Flutter.
        with patch("app.services.matcher.chat_completion",
                   return_value=_reply()) as call:
            role_suggest.suggest(PROFILE, "k", "https://x/v1", "m")

        prompt = call.call_args.args[0][0]["content"]
        assert "Flutter" in prompt
        assert "Backend Engineer" in prompt

    def test_duplicates_from_the_model_are_collapsed(self):
        outcome = _suggest(reply=_reply(
            {"title": "Flutter Developer", "why": "a"},
            {"title": "flutter developer", "why": "b"},
        ))
        assert len(outcome["suggestions"]) == 1

    def test_the_list_is_capped(self):
        outcome = _suggest(reply=_reply(*[
            {"title": f"Role {n}", "why": "x"} for n in range(30)
        ]))
        assert len(outcome["suggestions"]) <= role_suggest.MAX_SUGGESTIONS

    def test_an_empty_answer_is_a_valid_one(self):
        # "Your list already covers what the profile supports" is useful, and
        # inventing something to fill the space would not be.
        outcome = _suggest(reply=_reply())
        assert outcome["suggestions"] == []
        assert outcome["error"] is None


class TestItKnowsWhatTheTitleGateAlreadyAdmits:
    def test_a_role_sharing_a_word_is_marked_as_covered(self):
        # "Platform Engineer" against a profile targeting "Backend Engineer":
        # the gate passes on the shared word, so adding it changes nothing.
        outcome = _suggest(reply=_reply(
            {"title": "Platform Engineer", "why": "x"},
        ))
        assert outcome["suggestions"][0]["covered"] is True

    def test_a_genuinely_new_role_is_not(self):
        outcome = _suggest(reply=_reply(
            {"title": "Flutter Developer", "why": "x"},
        ))
        assert outcome["suggestions"][0]["covered"] is False

    def test_new_roles_come_first(self):
        # The covered ones are context. The answer is the ones that change
        # something.
        outcome = _suggest(reply=_reply(
            {"title": "Platform Engineer", "why": "x"},
            {"title": "Flutter Developer", "why": "y"},
        ))
        assert [s["title"] for s in outcome["suggestions"]] == [
            "Flutter Developer", "Platform Engineer",
        ]

    def test_seniority_words_do_not_make_a_role_look_new(self):
        # "Senior Backend Engineer" is the same gate as "Backend Engineer".
        assert role_suggest.already_covered(
            "Senior Backend Engineer", ["Backend Engineer"]
        ) is True

    def test_it_agrees_with_the_matcher(self):
        # The check here mirrors `matcher._title_matches_roles`. If the two
        # drift, this marks a role as new that the gate already admits, and
        # the user adds something with no effect.
        from app.services.matcher import _title_matches_roles

        roles = ["Backend Engineer"]
        for title in ("Platform Engineer", "Backend Developer",
                      "Flutter Developer", "Data Scientist"):
            assert role_suggest.already_covered(title, roles) == \
                _title_matches_roles(title, roles), title


class TestItNeverBreaksThePage:
    def test_a_provider_outage_is_reported_not_raised(self):
        with patch("app.services.matcher.chat_completion",
                   side_effect=RuntimeError("down")):
            outcome = role_suggest.suggest(PROFILE, "k", "https://x/v1", "m")

        assert outcome["suggestions"] == []
        assert "down" in outcome["error"]

    def test_unparseable_json_is_reported_not_raised(self):
        outcome = _suggest(reply="I think you'd like being a pilot")
        assert outcome["suggestions"] == []
        assert outcome["error"]

    def test_a_fenced_reply_is_still_read(self):
        outcome = _suggest(reply='```json\n{"suggestions": '
                                 '[{"title": "Flutter Developer", "why": "x"}]}\n```')
        assert outcome["suggestions"][0]["title"] == "Flutter Developer"

    def test_junk_entries_are_skipped_not_fatal(self):
        outcome = _suggest(reply=json.dumps({"suggestions": [
            "not a dict", {"why": "no title"},
            {"title": "Flutter Developer", "why": "x"},
        ]}))
        assert [s["title"] for s in outcome["suggestions"]] == ["Flutter Developer"]

    def test_an_empty_profile_says_what_to_do_instead_of_guessing(self):
        outcome = role_suggest.suggest({}, "k", "https://x/v1", "m")
        assert outcome["suggestions"] == []
        assert "skills" in outcome["error"]


class TestAddingOne:
    def test_it_appends(self):
        assert role_suggest.add_role(PROFILE, "Flutter Developer") == [
            "Backend Engineer", "Flutter Developer",
        ]

    def test_it_will_not_add_the_same_role_twice(self):
        assert role_suggest.add_role(PROFILE, "backend engineer") == [
            "Backend Engineer",
        ]

    def test_blank_input_changes_nothing(self):
        assert role_suggest.add_role(PROFILE, "   ") == ["Backend Engineer"]


class TestOnThePage:
    @pytest.fixture
    def profile_row(self, db):
        row = Profile(data=dict(PROFILE))
        db.add(row)
        db.commit()
        return row

    def test_the_button_is_on_the_skills_tab(self, client, db, profile_row):
        body = client.get("/profile?tab=skills").text
        assert "/profile/roles/suggest" in body
        assert "Suggest roles from my profile" in body

    def test_pressing_it_renders_the_suggestions(self, client, db, profile_row):
        with patch("app.services.matcher.chat_completion", return_value=_reply(
            {"title": "Flutter Developer", "why": "The profile lists Flutter."},
        )):
            body = client.post("/profile/roles/suggest").text

        assert "Flutter Developer" in body
        assert "The profile lists Flutter." in body

    def test_it_says_what_adding_a_role_costs(self, client, db, profile_row):
        # The gate matches on one shared word, so a broad addition is not a
        # small one — and that belongs where the decision is made.
        with patch("app.services.matcher.chat_completion", return_value=_reply(
            {"title": "Flutter Developer", "why": "x"},
        )):
            body = client.post("/profile/roles/suggest").text

        assert "one word in common" in body

    def test_a_covered_role_is_shown_without_an_add_button(self, client, db,
                                                            profile_row):
        with patch("app.services.matcher.chat_completion", return_value=_reply(
            {"title": "Platform Engineer", "why": "x"},
        )):
            body = client.post("/profile/roles/suggest").text

        assert "already gets through" in body

    def test_accepting_one_saves_it(self, client, db, profile_row):
        client.post("/profile/roles/add", data={"title": "Flutter Developer"})
        db.refresh(profile_row)

        assert "Flutter Developer" in profile_row.data["target_roles"]

    def test_accepting_one_returns_the_updated_form(self, client, db, profile_row):
        body = client.post("/profile/roles/add",
                           data={"title": "Flutter Developer"}).text

        assert "Flutter Developer" in body
        assert 'name="target_roles"' in body

    def test_an_outage_shows_a_message_rather_than_a_broken_tab(self, client, db,
                                                                 profile_row):
        with patch("app.services.matcher.chat_completion",
                   side_effect=RuntimeError("provider down")):
            response = client.post("/profile/roles/suggest")

        assert response.status_code == 200
        assert "provider down" in response.text
