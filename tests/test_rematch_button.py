"""
Scoring one job again, by hand.

Enrichment re-queues jobs on its own when their description grows — but only
for verdicts it reached by reading one, and never for a job that already has an
application. This is the manual door: a posting you think was judged wrongly,
re-read against the profile as it stands today.

It calls the real `match_job` rather than a scoring path of its own, and most
of these tests are about what that buys: the verdict lands in the score
history, the old one survives beside it, and a failure leaves the job exactly
as it was.
"""

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.models.job import Job, JobStatus
from app.models.profile import Profile
from app.services import score_history

PROFILE = {
    "target_roles": ["Backend Engineer"],
    "skills": {"lang": ["Python", "Go"]},
    "experience": [{"role": "Engineer", "company": "Acme",
                    "start_date": "Jan 2019", "end_date": "Aug 2026"}],
    "min_match_score": 60,
}


def _reply(score, reasoning="because"):
    return {"score": score, "reasoning": reasoning, "matched_skills": ["Python"],
            "missing_skills": [], "seniority_fit": True, "scored_by": "nim/glm"}


@pytest.fixture
def job(db):
    db.add(Profile(data=PROFILE))
    row = Job(
        source="greenhouse", source_urls=[f"https://x/{uuid.uuid4()}"],
        title="Backend Engineer", company="Acme", location="Remote",
        url=f"https://x/{uuid.uuid4()}",
        description="We need a backend engineer with Python and Go. " * 20,
        status=JobStatus.filtered_out, filter_reason="low_score",
        filter_detail="AI scored this 45/100, below your minimum of 60.",
        llm_score=45, matched_by="nim/glm",
        fetched_at=datetime.now(timezone.utc), dedupe_hash=uuid.uuid4().hex,
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture(autouse=True)
def _no_detail_extraction(monkeypatch):
    from app.services import job_details

    monkeypatch.setattr(job_details, "needs_extraction", lambda job: False)


def _score(client, job, score=82):
    with patch("app.services.matcher.llm_score_job", return_value=_reply(score)), \
         patch("app.llm.providers.deep_matching_provider", return_value=None):
        return client.post(f"/jobs/{job.id}/rematch")


class TestItRescoresTheJob:
    def test_a_rejected_job_can_be_rescued(self, client, db, job):
        response = _score(client, job, 82)

        db.refresh(job)
        assert response.status_code == 200
        assert job.status == JobStatus.matched
        assert job.llm_score == 82
        # The old explanation must go with the old verdict.
        assert job.filter_reason is None
        assert job.filter_detail is None

    def test_it_returns_the_updated_card(self, client, db, job):
        # The whole point of doing this synchronously: you pressed the button
        # because you disagreed with a number, so you get to see the new one.
        body = _score(client, job, 82).text
        assert "82" in body
        assert f'id="job-{job.id}"' in body

    def test_a_still_bad_job_stays_filtered(self, client, db, job):
        _score(client, job, 30)

        db.refresh(job)
        assert job.status == JobStatus.filtered_out
        assert job.filter_reason == "low_score"
        assert "30" in (job.filter_detail or "")

    def test_a_matched_job_can_be_scored_again_too(self, client, db, job):
        job.status = JobStatus.matched
        job.filter_reason = None
        db.commit()

        _score(client, job, 25)

        db.refresh(job)
        assert job.status == JobStatus.filtered_out

    def test_an_unknown_job_is_a_404(self, client, db):
        assert client.post(f"/jobs/{uuid.uuid4()}/rematch").status_code == 404


class TestWhatCallingTheRealMatcherBuys:
    def test_the_verdict_lands_in_the_score_history(self, client, db, job):
        _score(client, job, 82)

        rows = score_history.history(db, job.id)
        assert rows[0].score == 82
        assert rows[0].status == "matched"

    def test_the_old_verdict_survives_beside_it(self, client, db, job):
        # A re-score you asked for is exactly the case where the number it
        # replaced is worth keeping.
        _score(client, job, 45)
        _score(client, job, 82)

        assert [row.score for row in score_history.history(db, job.id)] == [82, 45]

    def test_a_close_call_still_gets_a_second_opinion(self, client, db, job):
        from app.llm.providers import Provider

        deep = json.dumps({"score": 88, "reasoning": "second pass",
                           "matched_skills": ["Go"], "missing_skills": [],
                           "seniority_fit": True})
        with patch("app.services.matcher.llm_score_job", return_value=_reply(62)), \
             patch("app.llm.providers.deep_matching_provider",
                   return_value=Provider(name="anthropic", api_key="k", model="opus")), \
             patch("app.services.matcher.call_provider", return_value=deep):
            client.post(f"/jobs/{job.id}/rematch")

        db.refresh(job)
        assert job.llm_score_deep == 88
        assert job.effective_score == 88

    def test_the_language_gate_still_applies(self, client, db, job):
        job.language = "de"
        db.commit()

        _score(client, job, 95)

        db.refresh(job)
        assert job.filter_reason == "language"


class TestFailureLeavesTheJobAlone:
    def test_a_provider_outage_is_a_503_and_no_change(self, client, db, job):
        from app.services.matcher import LLMUnavailableError

        with patch("app.services.matcher.llm_score_job",
                   side_effect=LLMUnavailableError("all providers down")):
            response = client.post(f"/jobs/{job.id}/rematch")

        db.refresh(job)
        assert response.status_code == 503
        # `match_job` leaves it `new` to be retried by the scheduled pass.
        assert job.llm_score == 45
        assert score_history.history(db, job.id) == []

    def test_an_unexpected_error_rolls_back(self, client, db, job):
        # A failed re-score must not leave the job worse off than not pressing
        # the button at all.
        with patch("app.services.matcher.llm_score_job",
                   side_effect=RuntimeError("boom")):
            response = client.post(f"/jobs/{job.id}/rematch")

        db.refresh(job)
        assert response.status_code == 502
        assert job.status == JobStatus.filtered_out
        assert job.llm_score == 45


class TestTheButton:
    def test_it_is_on_every_card(self, client, db, job):
        # Including `new` jobs: "score it now" is useful before the scheduled
        # pass gets there, not only after it has been wrong.
        body = client.get("/jobs").text
        assert f'hx-post="/jobs/{job.id}/rematch"' in body
        assert "Re-match" in body

    def test_it_shows_that_it_is_working(self, client, db, job):
        # It costs an LLM call, so a button that looked inert for five seconds
        # would get pressed three times.
        body = client.get("/jobs").text
        assert f'hx-indicator="#rematch-{job.id}"' in body
        assert 'hx-disabled-elt="this"' in body

    def test_the_indicator_css_matches_the_element_htmx_marks(self):
        # htmx puts `htmx-request` on the indicator itself when it is named by
        # hx-indicator, so the descendant rule alone never fires and the
        # spinner silently never appears.
        css = open("app/static/css/main.css").read()
        assert ".htmx-request.htmx-indicator" in css
