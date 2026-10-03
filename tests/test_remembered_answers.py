"""
Answers typed into application questions the profile does not cover, kept on
the user's own server when they press "Remember my answers", sent back to the
next fill, listed on the profile's Screening tab and forgotten from there.
"""

import pytest

from app.models.profile import Profile
from app.services import remembered_answers
from tests.test_overlay_actions import agent, auth  # noqa: F401 — fixture reuse


def _profile(db, data=None):
    db.add(Profile(data=data or {"personal": {"name": "Jane Doe"}}))
    db.commit()


def test_answers_are_kept_and_come_back_to_the_next_fill(agent, db):  # noqa: F811
    _profile(db)
    reply = agent.post("/api/agent/remember-answers", headers=auth(), json={"answers": [
        {"question": "Are you open to hybrid work? *", "answer": "Yes, three days a week"},
        {"question": "Do you have a non-compete agreement?", "answer": "No"},
    ]}).json()
    assert reply == {"ok": True, "saved": 2}
    body = agent.get("/api/agent/autofill-fields", headers=auth()).json()
    assert body["remembered"] == {
        "are you open to hybrid work": "Yes, three days a week",
        "do you have a non compete agreement": "No",
    }


def test_it_needs_the_token(agent, db):  # noqa: F811
    _profile(db)
    assert agent.post("/api/agent/remember-answers", json={"answers": []}).status_code == 401


def test_credentials_and_long_text_are_refused_whoever_sends_them():
    data, saved = remembered_answers.remember({}, [
        {"question": "Social Security Number", "answer": "123-45-6789"},
        {"question": "Create a password", "answer": "hunter2"},
        {"question": "Why this company?", "answer": "x" * 600},
        {"question": "", "answer": "orphan"},
        "not a dict",
        {"question": "Preferred pronouns", "answer": "she/her"},
    ])
    assert saved == 1
    assert list(remembered_answers.lookup(data)) == ["preferred pronouns"]


def test_a_later_answer_replaces_an_earlier_one_and_the_store_is_bounded(monkeypatch):
    data, _ = remembered_answers.remember({}, [{"question": "Relocate?", "answer": "No"}])
    data, _ = remembered_answers.remember(data, [{"question": "relocate", "answer": "Yes"}])
    assert remembered_answers.lookup(data) == {"relocate": "Yes"}
    monkeypatch.setattr(remembered_answers, "MAX_ANSWERS", 3)
    for n in range(5):
        data, _ = remembered_answers.remember(data, [{"question": f"Q{n}", "answer": "A"}])
    assert len(remembered_answers.entries(data)) == 3


def test_the_screening_tab_lists_them_and_forgets_one(client, db):
    data, _ = remembered_answers.remember({"personal": {}}, [
        {"question": "Are you open to hybrid work?", "answer": "Yes"},
        {"question": "Willing to relocate?", "answer": "No"},
    ])
    _profile(db, data)
    page = client.get("/profile?tab=screening").text
    assert "Are you open to hybrid work?" in page and "Willing to relocate?" in page

    page = client.post("/profile/remembered/forget",
                       data={"key": "are you open to hybrid work"}).text
    assert "Are you open to hybrid work?" not in page and "Willing to relocate?" in page
    profile = db.query(Profile).first()
    db.refresh(profile)
    assert list(remembered_answers.lookup(profile.data)) == ["willing to relocate"]


def test_the_self_identification_answer_reaches_the_fill(agent, db):  # noqa: F811
    _profile(db, {"personal": {}, "screening_answers": {
        "eeo_self_identification": "Decline to self-identify"}})
    body = agent.get("/api/agent/autofill-fields", headers=auth()).json()
    assert body["eeo_self_identification"] == "Decline to self-identify"
def test_saved_answers_are_scoped_to_an_employer_path_and_expire():
    from datetime import datetime, timedelta, timezone
    from app.services import remembered_answers
    data, _ = remembered_answers.remember({}, [{"question": "Are you available to start soon?", "answer": "Yes"}], "https://jobs.lever.co/acme/123")
    assert remembered_answers.lookup(data, "https://jobs.lever.co/acme/456")
    assert not remembered_answers.lookup(data, "https://jobs.lever.co/other/456")
    assert not remembered_answers.lookup(data, "https://jobs.lever.co/acme/456", now=datetime.now(timezone.utc) + timedelta(days=31))


def test_unscoped_answers_are_not_reused_on_a_named_employer():
    data, _ = remembered_answers.remember({}, [{"question": "Preferred name?", "answer": "Employer name"}], "https://jobs.lever.co/acme/123")
    data, _ = remembered_answers.remember(data, [{"question": "Preferred name?", "answer": "Generic name"}])
    assert remembered_answers.lookup(data, "https://jobs.lever.co/acme/456")["preferred name"] == "Employer name"
    assert not remembered_answers.lookup(data, "https://jobs.lever.co/other/456")
    assert remembered_answers.lookup(data)["preferred name"] == "Generic name"


@pytest.mark.parametrize("first,same_employer,other_employer", [
    ("https://jobs.dayforcehcm.com/en-US/taiho/CANDIDATEPORTAL/jobs/4707",
     "https://jobs.dayforcehcm.com/en-US/taiho/CANDIDATEPORTAL/jobs/4708",
     "https://jobs.dayforcehcm.com/en-US/texasfarm/CANDIDATEPORTAL/jobs/634"),
    ("https://boards.greenhouse.io/embed/job_app?for=acme&token=123",
     "https://boards.greenhouse.io/embed/job_app?token=456&for=acme",
     "https://boards.greenhouse.io/embed/job_app?for=other&token=789"),
    ("https://recruiting.paylocity.com/Recruiting/Jobs/All/acme",
     "https://recruiting.paylocity.com/Recruiting/Jobs/All/acme/Acme-Inc",
     "https://recruiting.paylocity.com/Recruiting/Jobs/All/other"),
    ("https://ats.rippling.com/en-US/acme/jobs/123",
     "https://ats.rippling.com/acme/jobs/456",
     "https://ats.rippling.com/en-US/other/jobs/789"),
    ("https://jobs.jobvite.com/careers/acme/job/123",
     "https://jobs.jobvite.com/acme/job/456",
     "https://jobs.jobvite.com/careers/other/job/789"),
])
def test_shared_ats_hosts_keep_employer_answers_separate(first, same_employer, other_employer):
    data, saved = remembered_answers.remember({}, [{"question": "Have you worked for us before?", "answer": "Yes"}], first)
    assert saved == 1
    assert remembered_answers.lookup(data, same_employer) == {"have you worked for us before": "Yes"}
    assert remembered_answers.lookup(data, other_employer) == {}


@pytest.mark.parametrize("first,other", [
    ("https://recruiting.paylocity.com/Recruiting/Jobs/Details/123",
     "https://recruiting.paylocity.com/Recruiting/Jobs/Details/456"),
    ("https://boards.greenhouse.io/embed/job_app?token=123",
     "https://boards.greenhouse.io/embed/job_app?token=456"),
    ("https://shared.example.com/apply?employer=acme", "https://shared.example.com/apply?employer=other"),
])
def test_uncertain_employer_identity_reuses_only_the_same_posting(first, other):
    data, _ = remembered_answers.remember({}, [{"question": "Have you worked for us before?", "answer": "Yes"}], first)
    assert remembered_answers.lookup(data, first)
    assert not remembered_answers.lookup(data, other)


@pytest.mark.parametrize("scope", ["jobs.dayforcehcm.com/en-US", "boards.greenhouse.io/embed", ""])
def test_unsafe_legacy_scopes_are_not_reused(scope):
    data = {"remembered_answers": {"old": {"question": "Have you worked for us before?", "answer": "Yes", "scope": scope}}}
    assert not remembered_answers.lookup(data, "https://jobs.dayforcehcm.com/en-US/other/CANDIDATEPORTAL/jobs/1")
    assert not remembered_answers.lookup(data, "https://boards.greenhouse.io/embed/job_app?for=other&token=1")


def test_an_invalid_site_does_not_turn_an_answer_into_a_global_one():
    data, saved = remembered_answers.remember({}, [{"question": "Have you worked for us before?", "answer": "Yes"}], "not a URL")
    assert saved == 0 and not remembered_answers.lookup(data)
