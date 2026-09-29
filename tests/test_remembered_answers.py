"""
Answers typed into application questions the profile does not cover, kept on
the user's own server when they press "Remember my answers", sent back to the
next fill, listed on the profile's Screening tab and forgotten from there.
"""

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
    data, _ = remembered_answers.remember({}, [{"question": "Are you available to start soon?", "answer": "Yes"}], "https://jobs.example.com/acme/123")
    assert remembered_answers.lookup(data, "https://jobs.example.com/acme/456")
    assert not remembered_answers.lookup(data, "https://jobs.example.com/other/456")
    assert not remembered_answers.lookup(data, "https://jobs.example.com/acme/456", now=datetime.now(timezone.utc) + timedelta(days=31))


def test_employer_answer_takes_precedence_over_a_later_generic_answer():
    data, _ = remembered_answers.remember({}, [{"question": "Preferred name?", "answer": "Employer name"}], "https://jobs.example.com/acme/123")
    data, _ = remembered_answers.remember(data, [{"question": "Preferred name?", "answer": "Generic name"}])
    assert remembered_answers.lookup(data, "https://jobs.example.com/acme/456")["preferred name"] == "Employer name"
    assert remembered_answers.lookup(data, "https://jobs.example.com/other/456")["preferred name"] == "Generic name"
