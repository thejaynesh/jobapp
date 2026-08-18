"""
Postings written in a language the candidate cannot read.

Arbeitnow returns German listings under English titles — "Senior Software
Engineer" over a description that is entirely in German — so the title gate
passes them and a model is then asked to score a posting nobody involved could
act on. That is two LLM calls each (three when the second opinion runs), spent
to arrive at a conclusion a two-letter field already knew.

The care here is all in the direction of the failure: a wrongly-skipped job is
one the user never sees, so anything uncertain is scored.
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.config import settings
from app.models.job import Job, JobStatus
from app.models.profile import Profile
from app.services import matcher

PROFILE = {
    "target_roles": ["Backend Engineer"],
    "skills": {"lang": ["Python", "Go"]},
    "experience": [{"role": "Engineer", "company": "Acme",
                    "start_date": "Jan 2019", "end_date": "Aug 2026"}],
    "min_match_score": 60,
}


def _job(**kwargs) -> Job:
    defaults = dict(
        source="arbeitnow", source_urls=[f"https://x/{uuid.uuid4()}"],
        title="Backend Engineer", company="Acme",
        url=f"https://x/{uuid.uuid4()}",
        description="We need a backend engineer with Python and Go. " * 20,
        status=JobStatus.new, fetched_at=datetime.now(timezone.utc),
        dedupe_hash=uuid.uuid4().hex,
    )
    defaults.update(kwargs)
    return Job(**defaults)


def _outcome(job, profile=PROFILE):
    return matcher.evaluate_keyword_filter(job, profile)


class TestTheGate:
    def test_a_german_posting_is_dropped(self, db):
        outcome = _outcome(_job(language="de"))

        assert outcome.passed is False
        assert outcome.reason == "language"
        assert "German" in outcome.detail
        assert "English" in outcome.detail

    def test_an_english_posting_passes(self, db):
        assert _outcome(_job(language="en")).passed is True

    def test_an_unknown_language_passes(self, db):
        # The field is filled by the detail extraction, which only runs on
        # descriptions long enough to read. Treating unknown as foreign would
        # silently drop the entire backlog on a column that was never filled.
        assert _outcome(_job(language=None)).passed is True
        assert _outcome(_job(language="")).passed is True
        assert _outcome(_job(language="   ")).passed is True

    def test_a_regional_tag_is_still_english(self, db):
        # A model that answers "en-GB" should not cost a posting.
        for code in ("en-GB", "en_US", "EN", "en-us"):
            assert _outcome(_job(language=code)).passed is True, code

    def test_the_reason_names_the_language_rather_than_the_code(self, db):
        # "de" is a two-letter puzzle on a job card; "German" is a sentence.
        assert "German" in _outcome(_job(language="de")).detail
        assert "Japanese" in _outcome(_job(language="ja")).detail

    def test_an_unrecognised_code_still_reads(self, db):
        detail = _outcome(_job(language="xh")).detail
        assert "xh" in detail

    def test_it_comes_before_the_skills_check(self, db):
        # Being told a Stellenausschreibung has "too few skills" is a true
        # statement that explains nothing.
        job = _job(language="de", description="Wir suchen einen Entwickler. " * 20)
        assert _outcome(job).reason == "language"

    def test_a_title_mismatch_still_wins(self, db):
        # The cheaper, more specific rejection keeps precedence.
        assert _outcome(_job(language="de", title="Head Chef")).reason == \
            "title_mismatch"


class TestTheSwitch:
    def test_nothing_is_dropped_when_it_is_off(self, db):
        profile = {**PROFILE, "settings": {"filter_by_language": False}}
        assert _outcome(_job(language="de"), profile).passed is True

    def test_the_accepted_list_is_configurable(self, db, monkeypatch):
        monkeypatch.setattr(settings, "MATCH_LANGUAGES", "en, de")
        assert _outcome(_job(language="de")).passed is True
        assert _outcome(_job(language="fr")).passed is False

    def test_an_empty_list_falls_back_to_english(self, db, monkeypatch):
        # A blank setting must not mean "accept nothing", which would filter
        # every job in the database.
        monkeypatch.setattr(settings, "MATCH_LANGUAGES", "")
        assert matcher.accepted_languages() == {"en"}
        assert _outcome(_job(language="en")).passed is True


class TestInsideAMatch:
    """The second gate: language is only learned when details are extracted."""

    def _run(self, db, job, language):
        db.add(Profile(data=PROFILE))
        db.add(job)
        db.commit()

        def _extract(target):
            target.language = language
            target.details_extracted_at = datetime.now(timezone.utc)

        reply = {"score": 82, "reasoning": "fits", "matched_skills": ["Python"],
                 "missing_skills": [], "seniority_fit": True,
                 "scored_by": "nim/glm"}
        with patch("app.services.job_details.needs_extraction", return_value=True), \
             patch("app.services.job_details.extract_and_apply", side_effect=_extract), \
             patch("app.services.matcher.llm_score_job", return_value=reply) as score, \
             patch("app.llm.providers.deep_matching_provider", return_value=None):
            outcome = matcher.match_job(db, job, PROFILE, "k", "u", "m")
        db.commit()
        return outcome, score

    def test_a_posting_found_to_be_german_never_reaches_the_model(self, db):
        # The saving this exists for. The gate above could not have seen the
        # language, because nothing had read the description yet.
        job = _job(language=None)
        outcome, score = self._run(db, job, "de")

        assert outcome == "filtered_out"
        assert job.filter_reason == "language"
        score.assert_not_called()

    def test_an_english_posting_is_scored_as_usual(self, db):
        job = _job(language=None)
        _, score = self._run(db, job, "en")
        score.assert_called_once()

    def test_a_posting_found_to_be_german_keeps_no_stale_score(self, db):
        # `effective_score` reads llm_score_deep first, so leaving it behind
        # would show a score beside "filtered out" that this pass never gave.
        job = _job(language=None, llm_score=62, llm_score_deep=88,
                   deep_matched_by="anthropic/opus")
        self._run(db, job, "de")

        assert job.llm_score is None
        assert job.llm_score_deep is None
        assert job.effective_score is None

    def test_the_verdict_is_recorded_in_the_score_history(self, db):
        from app.services import score_history

        job = _job(language=None)
        self._run(db, job, "de")

        rows = score_history.history(db, job.id)
        assert len(rows) == 1
        assert rows[0].filter_reason == "language"
        assert rows[0].score is None


class TestItIsNotReScored:
    def test_a_fuller_description_does_not_bring_it_back(self, db):
        # Enrichment re-queues verdicts that were reached by reading the
        # description. More German is still German.
        assert "language" not in matcher.DESCRIPTION_DEPENDENT_REASONS

    def test_the_reason_has_a_label_for_the_ui(self, db):
        assert matcher.FILTER_REASON_LABELS["language"]


class TestOnThePages:
    def test_the_funnel_names_it(self, client, db):
        job = _job(language="de", status=JobStatus.filtered_out,
                   filter_reason="language")
        db.add(job)
        db.commit()

        body = client.get("/funnel").text
        assert "Posting isn&#39;t written in a language you read" in body \
            or "Posting isn't written in a language you read" in body
