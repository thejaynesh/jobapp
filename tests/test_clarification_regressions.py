from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest

from app.models.profile import Profile
from app.services import evidence
from tests.test_application_history import application


AUTHORIZATION = "You must be authorized to work in the United States."


def requirement_job(description=AUTHORIZATION):
    return SimpleNamespace(description=description, required_skills=[], nice_to_have_skills=[])


@pytest.mark.parametrize("text", [
    "I am not authorized to work in the United States.",
    "I am unsure whether my authorization permits this job.",
    "I can only work in Canada.",
])
def test_legacy_free_text_cannot_prove_eligibility(text):
    job = requirement_job()
    key = evidence.requirements(job)[0]["id"]
    profile = {"requirement_answers": {key: {"text": text}}}
    assert evidence.assess(job, profile)["requirements"][0]["status"] == "unknown"


@pytest.mark.parametrize("description", [
    AUTHORIZATION, "At least 5 years of experience.", "A degree is required.",
])
@pytest.mark.parametrize("satisfaction,status", [
    ("meets", "supported"), ("does_not_meet", "conflicting"), ("unsure", "unknown"),
])
def test_non_skill_requirements_use_the_explicit_answer(description, satisfaction, status):
    job = requirement_job(description)
    key = evidence.requirements(job)[0]["id"]
    profile = {"requirement_answers": {key: {
        "text": "My circumstances are described in the profile.", "satisfaction": satisfaction,
    }}}
    assert evidence.assess(job, profile)["requirements"][0]["status"] == status


def test_changing_only_the_explicit_answer_invalidates_cached_evidence():
    job = requirement_job()
    key = evidence.requirements(job)[0]["id"]
    profile = {"requirement_answers": {key: {"text": "See my authorization details.", "satisfaction": "meets"}}}
    job.match_assessment = evidence.assess(job, profile)
    profile["requirement_answers"][key]["satisfaction"] = "does_not_meet"
    assert evidence.current(job, profile)["requirements"][0]["status"] == "conflicting"


def test_old_assessment_version_is_recomputed():
    job = requirement_job()
    key = evidence.requirements(job)[0]["id"]
    profile = {"requirement_answers": {key: {"text": "I am not authorized."}}}
    job.match_assessment = evidence.assess(job, profile)
    job.match_assessment["version"] = 2
    job.match_assessment["requirements"][0]["status"] = "supported"
    assert evidence.current(job, profile)["requirements"][0]["status"] == "unknown"


def test_legacy_untyped_answers_expire_but_new_skills_do_not():
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    answers = {
        "legacy": {"text": "I can work here", "at": old},
        "eligibility": {"text": "I can work here", "kind": "eligibility", "at": old},
        "skill": {"text": "Built Python services", "kind": "skill", "at": old},
    }
    assert set(evidence.active_answers({"requirement_answers": answers})) == {"skill"}


class AnswerForms(HTMLParser):
    """Submit the hidden values the actual page supplies, like a browser."""

    def __init__(self, html):
        super().__init__()
        self.forms = []
        self.current = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form" and attrs.get("action") == "/today/answer":
            self.current = {}
        if tag == "input" and self.current is not None and attrs.get("name"):
            self.current[attrs["name"]] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form" and self.current is not None:
            self.forms.append(self.current)
            self.current = None


@pytest.mark.parametrize("page", ["evidence", "today"])
def test_both_clarification_forms_save_expiring_explicit_eligibility(client, db, page):
    app = application(db)
    app.job.description = AUTHORIZATION
    db.add(Profile(data={"settings": {"answer_expiry_days": 7}}))
    db.commit()
    url = f"/jobs/{app.job_id}/evidence" if page == "evidence" else "/today"
    response = client.get(url)
    assert response.status_code == 200
    form = AnswerForms(response.text).forms[0]
    assert form["question_kind"] == "eligibility"
    assert form["job_id"] == str(app.job_id)
    response = client.post("/today/answer", data={
        **form, "answer": "I am not authorized to work in the United States.",
        "satisfaction": "does_not_meet",
    }, follow_redirects=False)
    assert response.status_code == 303
    profile = db.query(Profile).first().data
    saved = profile["requirement_answers"][form["question_id"]]
    assert datetime.fromisoformat(saved["expires_at"]) - datetime.fromisoformat(saved["at"]) == timedelta(days=7)
    assert evidence.current(app.job, profile)["requirements"][0]["status"] == "conflicting"


def test_requirement_kind_comes_from_the_posting_when_client_omits_it(client, db):
    app = application(db)
    app.job.description = AUTHORIZATION
    db.commit()
    key = evidence.requirements(app.job)[0]["id"]
    response = client.post("/today/answer", data={
        "job_id": str(app.job_id), "question_id": key,
        "answer": "My authorization permits this role.", "satisfaction": "meets",
    }, follow_redirects=False)
    assert response.status_code == 303
    saved = db.query(Profile).first().data["requirement_answers"][key]
    assert saved["kind"] == "eligibility" and saved["expires_at"]


def test_changed_requirement_cannot_save_an_answer_to_an_obsolete_question(client, db):
    app = application(db)
    app.job.description = AUTHORIZATION
    key = evidence.requirements(app.job)[0]["id"]
    app.job.description = "A degree is required."
    db.commit()
    response = client.post("/today/answer", data={
        "job_id": str(app.job_id), "question_id": key, "answer": "An old answer.",
    }, follow_redirects=False)
    assert response.status_code == 422
