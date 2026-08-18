"""
What the extension is given to type into an application form.

The endpoint is a narrow projection on purpose. These values end up in an
employer's page — unavoidably, since typing them into a form is the point — so
what travels is only what a form asks for. The tests that matter here are the
ones asserting what does *not* travel.

The field-matching rules themselves live in the extension and are exercised in
a browser; what is checked here is that the server hands over the right shape
and nothing more.
"""

import pytest

from app.config import settings
from app.models.profile import Profile

TOKEN = "test-agent-token-value"

FULL_PROFILE = {
    "personal": {
        "name": "Jaynesh Bhandari",
        "email": "someone@example.com",
        "phone": "+1 (207) 555-0100",
        "linkedin": "https://www.linkedin.com/in/example",
        "github": "https://github.com/example",
        "website": "https://example.com",
        "location": "Boston, MA",
    },
    "education": [
        {"school": "Northeastern University", "degree": "Master of Science",
         "field": "Computer Science", "gpa": "3.7"},
        {"school": "Somewhere Else", "degree": "Bachelor of Engineering",
         "field": "Information Technology"},
    ],
    # None of the below should reach an employer's page.
    "narrative": {"summary": "A private summary of my career.",
                  "answers": ["Something personal."]},
    "latex_template": "\\documentclass{article}",
    "min_match_score": 70,
    "excluded_companies": ["Some Company I Dislike"],
    "ats_slug_cache": {"greenhouse": ["acme"]},
}


@pytest.fixture
def agent(client, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_TOKEN", TOKEN)
    monkeypatch.setattr(settings, "APP_PASSWORD", "irrelevant-but-required")
    monkeypatch.setattr(settings, "SECRET_KEY", "not-the-placeholder-value")
    return client


def auth_header():
    return {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def profile(db):
    row = Profile(data=FULL_PROFILE)
    db.add(row)
    db.commit()
    return row


class TestAuthentication:
    def test_it_needs_the_agent_token(self, agent, profile):
        assert agent.get("/api/agent/autofill-fields").status_code == 401


class TestWhatItHandsOver:
    def test_the_contact_details_a_form_asks_for(self, agent, profile):
        body = agent.get("/api/agent/autofill-fields", headers=auth_header()).json()
        assert body["email"] == "someone@example.com"
        assert body["phone"] == "+1 (207) 555-0100"
        assert body["location"] == "Boston, MA"

    def test_the_name_split_for_forms_that_want_it_in_two_boxes(self, agent, profile):
        body = agent.get("/api/agent/autofill-fields", headers=auth_header()).json()
        assert body["first_name"] == "Jaynesh"
        assert body["last_name"] == "Bhandari"
        assert body["full_name"] == "Jaynesh Bhandari"

    def test_a_single_word_name_leaves_the_surname_empty(self, agent, db):
        db.add(Profile(data={"personal": {"name": "Prince"}}))
        db.commit()
        body = agent.get("/api/agent/autofill-fields", headers=auth_header()).json()
        assert body["first_name"] == "Prince"
        assert body["last_name"] == ""

    def test_a_three_part_name_keeps_the_remainder_together(self, agent, db):
        db.add(Profile(data={"personal": {"name": "Ana Maria Silva Costa"}}))
        db.commit()
        body = agent.get("/api/agent/autofill-fields", headers=auth_header()).json()
        assert body["first_name"] == "Ana"
        assert body["last_name"] == "Maria Silva Costa"

    def test_the_links(self, agent, profile):
        body = agent.get("/api/agent/autofill-fields", headers=auth_header()).json()
        assert "linkedin.com" in body["linkedin"]
        assert "github.com" in body["github"]

    def test_the_most_recent_education_only(self, agent, profile):
        # A form has one set of school boxes, and the newest degree is the one
        # it means.
        body = agent.get("/api/agent/autofill-fields", headers=auth_header()).json()
        assert body["school"] == "Northeastern University"
        assert body["degree"] == "Master of Science"
        assert body["field_of_study"] == "Computer Science"


class TestWhatItWithholds:
    def test_nothing_beyond_the_known_fields_is_sent(self, agent, profile):
        # An allow-list by construction: a field added to the profile later
        # must not start travelling to employers by default.
        body = agent.get("/api/agent/autofill-fields", headers=auth_header()).json()
        assert set(body) == {
            "first_name", "last_name", "full_name", "email", "phone", "location",
            "linkedin", "github", "website", "school", "degree", "field_of_study",
            # The screening answers are on this list deliberately: they are
            # written on the profile's Screening tab *in order* to be typed
            # into forms, which is not true of anything else in the profile.
            "work_authorization", "sponsorship_required", "start_date",
            "salary_expectation", "referral_source",
        }

    def test_the_narrative_stays_home(self, agent, profile):
        raw = agent.get("/api/agent/autofill-fields", headers=auth_header()).text
        assert "private summary" not in raw.lower()
        assert "Something personal" not in raw

    def test_preferences_and_templates_stay_home(self, agent, profile):
        raw = agent.get("/api/agent/autofill-fields", headers=auth_header()).text
        assert "documentclass" not in raw
        assert "Some Company I Dislike" not in raw
        assert "min_match_score" not in raw

    def test_the_gpa_is_not_volunteered(self, agent, profile):
        # Asked for sometimes, but not something to type in unprompted.
        body = agent.get("/api/agent/autofill-fields", headers=auth_header()).json()
        assert "gpa" not in body


class TestMissingData:
    def test_an_empty_profile_returns_blanks_rather_than_failing(self, agent, db):
        db.add(Profile(data={}))
        db.commit()
        response = agent.get("/api/agent/autofill-fields", headers=auth_header())
        assert response.status_code == 200
        assert response.json()["email"] == ""

    def test_no_profile_at_all_is_not_an_error(self, agent):
        response = agent.get("/api/agent/autofill-fields", headers=auth_header())
        assert response.status_code == 200
        assert response.json()["full_name"] == ""

    def test_values_are_trimmed(self, agent, db):
        db.add(Profile(data={"personal": {"email": "  spaced@example.com  "}}))
        db.commit()
        body = agent.get("/api/agent/autofill-fields", headers=auth_header()).json()
        assert body["email"] == "spaced@example.com"
