"""
The match-quality check.

Every prompt edit so far has been made on the strength of reading a few outputs
and deciding they looked better. That cannot catch a regression: a change that
improves ten jobs and quietly breaks forty looks exactly like a change that
improves ten jobs.

The labels are not hand-written from scratch — they are decisions the user has
already made. Applying to a job says it was a good fit; marking one "not
interested" says it was not.
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from app.models.application import Application, ApplicationStatus
from app.models.job import Job, JobStatus
from app.models.profile import Profile
from app.services import match_eval

LONG = "We need a backend engineer with deep Python and Go experience. " * 12


def _job(db, **kwargs) -> Job:
    defaults = dict(
        source="greenhouse",
        source_urls=[f"https://x/{uuid.uuid4()}"],
        title="Backend Engineer",
        company="Acme",
        location="Remote",
        url=f"https://x/{uuid.uuid4()}",
        description=LONG,
        status=JobStatus.new,
        fetched_at=datetime.now(timezone.utc),
        dedupe_hash=uuid.uuid4().hex,
    )
    defaults.update(kwargs)
    job = Job(**defaults)
    db.add(job)
    db.commit()
    return job


class TestBuildingLabelsFromWhatYouAlreadyDecided:
    def test_a_job_you_applied_to_is_a_good_fit(self, db):
        job = _job(db, title="Applied To This")
        db.add(Application(job_id=job.id, status=ApplicationStatus.applied))
        db.commit()

        labels = match_eval.build_labels(db)
        assert [(lab.verdict, lab.fields["title"]) for lab in labels] == [
            (match_eval.GOOD, "Applied To This")
        ]

    def test_a_job_you_marked_not_interested_is_a_bad_fit(self, db):
        _job(db, title="Rejected This", status=JobStatus.filtered_out,
             filter_reason="manual")

        labels = match_eval.build_labels(db)
        assert [(lab.verdict, lab.fields["title"]) for lab in labels] == [
            (match_eval.BAD, "Rejected This")
        ]

    def test_the_pipelines_own_verdicts_are_not_ground_truth(self, db):
        """
        Grading the matcher against its own past output measures nothing.
        """
        for reason in ("low_score", "few_skills", "title_mismatch", "seniority"):
            _job(db, status=JobStatus.filtered_out, filter_reason=reason)
        assert match_eval.build_labels(db) == []

    def test_an_application_nobody_ever_sent_is_not_approval(self, db):
        # Documents were generated and nothing happened. That is silence.
        job = _job(db)
        db.add(Application(job_id=job.id, status=ApplicationStatus.not_applied))
        db.commit()
        assert match_eval.build_labels(db) == []

    def test_an_employer_rejection_still_counts_as_wanting_the_job(self, db):
        # Their verdict on the candidate, not the candidate's on the job.
        job = _job(db)
        db.add(Application(job_id=job.id, status=ApplicationStatus.rejected))
        db.commit()
        assert [lab.verdict for lab in match_eval.build_labels(db)] == [match_eval.GOOD]

    def test_a_job_too_thin_to_judge_is_left_out(self, db):
        job = _job(db, description="Too short to be a real judgement.")
        db.add(Application(job_id=job.id, status=ApplicationStatus.applied))
        db.commit()
        assert match_eval.build_labels(db) == []

    def test_each_side_is_capped_so_the_set_stays_balanced(self, db):
        """
        A set that is 90% rejections scores 90% by agreeing with everything,
        and would report a matcher that says no to every job as excellent.
        """
        for i in range(6):
            _job(db, title=f"Rejected {i}", status=JobStatus.filtered_out,
                 filter_reason="manual")
        job = _job(db, title="Wanted")
        db.add(Application(job_id=job.id, status=ApplicationStatus.applied))
        db.commit()

        labels = match_eval.build_labels(db, limit_per_side=2)
        assert sum(1 for lab in labels if lab.verdict == match_eval.BAD) == 2
        assert sum(1 for lab in labels if lab.verdict == match_eval.GOOD) == 1


class TestTheFixtureFile:
    def _labels(self):
        return [
            match_eval.LabelledJob(
                verdict=match_eval.GOOD, note="you applied",
                fields={"title": "Backend Engineer", "company": "Acme",
                        "description": LONG, "required_years": 3},
            ),
            match_eval.LabelledJob(
                verdict=match_eval.BAD,
                fields={"title": "Dental Hygienist", "company": "Smiles",
                        "description": LONG},
            ),
        ]

    def test_it_round_trips(self, tmp_path):
        path = tmp_path / "labels.json"
        match_eval.save(self._labels(), {"target_roles": ["Backend Engineer"]}, path)
        labels, profile = match_eval.load(path)

        assert [lab.verdict for lab in labels] == [match_eval.GOOD, match_eval.BAD]
        assert labels[0].fields["required_years"] == 3
        assert profile == {"target_roles": ["Backend Engineer"]}

    def test_the_profile_is_frozen_with_it(self, tmp_path):
        """
        A score judges a job *and* a candidate. Without this, a run six weeks
        later measures the profile drifting as much as the prompt changing.
        """
        path = tmp_path / "labels.json"
        match_eval.save(self._labels(), {"skills": {"lang": ["Python"]}}, path)
        assert json.loads(path.read_text())["profile"] == {"skills": {"lang": ["Python"]}}

    def test_a_missing_file_says_how_to_make_one(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="--build"):
            match_eval.load(tmp_path / "nope.json")

    def test_a_bad_verdict_is_refused_by_name(self, tmp_path):
        path = tmp_path / "labels.json"
        path.write_text(json.dumps({"labels": [{"verdict": "maybe", "title": "x"}]}))
        with pytest.raises(ValueError, match="verdict"):
            match_eval.load(path)

    def test_an_empty_file_is_refused(self, tmp_path):
        path = tmp_path / "labels.json"
        path.write_text(json.dumps({"labels": []}))
        with pytest.raises(ValueError, match="no labels"):
            match_eval.load(path)

    def test_a_label_scores_without_the_database(self, tmp_path):
        """
        What makes the file worth committing: it has to score identically on a
        machine whose database has never seen these postings.
        """
        from app.services.matcher import _build_match_prompt

        path = tmp_path / "labels.json"
        match_eval.save(self._labels(), {"target_roles": ["Backend Engineer"]}, path)
        labels, profile = match_eval.load(path)

        user = _build_match_prompt(labels[0].as_job(), profile)[1]["content"]
        assert "Backend Engineer" in user
        assert "Required experience (stated in the posting): 3 years" in user


class TestRunningTheCheck:
    def _labels(self, verdicts):
        return [
            match_eval.LabelledJob(
                verdict=v,
                fields={"title": f"Job {i}", "company": "Acme", "description": LONG},
            )
            for i, v in enumerate(verdicts)
        ]

    def _run(self, verdicts, scores, threshold=70):
        replies = [(s, "ok") for s in scores]
        with patch("app.services.model_compare.score_with_model",
                   side_effect=replies):
            return match_eval.run(
                self._labels(verdicts), {}, model="test-model", threshold=threshold
            )

    def test_perfect_agreement(self):
        result = self._run(["good", "bad"], [90, 20])
        assert result["agreement"] == 100.0
        assert result["false_rejects"] == 0
        assert result["false_accepts"] == 0

    def test_a_false_reject_is_a_job_you_never_see(self):
        result = self._run(["good"], [40])
        assert result["false_rejects"] == 1
        assert result["agreement"] == 0.0

    def test_a_false_accept_costs_a_generation_and_your_attention(self):
        result = self._run(["bad"], [95])
        assert result["false_accepts"] == 1

    def test_the_two_directions_are_reported_separately(self):
        # They cost different things, so one number would hide which is which.
        result = self._run(["good", "bad", "good", "bad"], [30, 95, 90, 10])
        assert result["false_rejects"] == 1
        assert result["false_accepts"] == 1
        assert result["agreement"] == 50.0

    def test_the_threshold_is_what_decides(self):
        assert self._run(["good"], [72], threshold=70)["agreement"] == 100.0
        assert self._run(["good"], [72], threshold=80)["agreement"] == 0.0

    def test_an_unreadable_reply_is_not_counted_as_a_disagreement(self):
        """
        A model that never emits clean JSON scores nothing at all — that is a
        model problem, and folding it into the agreement rate would disguise it
        as a scoring one.
        """
        with patch("app.services.model_compare.score_with_model",
                   side_effect=[(None, "unreadable"), (90, "ok")]):
            result = match_eval.run(self._labels(["good", "good"]), {},
                                    model="m", threshold=70)
        assert result["unreadable"] == 1
        assert result["scored"] == 1
        assert result["agreement"] == 100.0

    def test_disagreements_name_the_job_and_the_score(self):
        result = self._run(["good"], [40])
        assert result["disagreements"][0]["score"] == 40
        assert result["disagreements"][0]["title"] == "Job 0"

    def test_the_report_reads_as_something_you_would_paste_in(self):
        text = match_eval.format_report(self._run(["good", "bad"], [40, 95]))
        assert "AGREEMENT" in text
        assert "false rejects    1" in text
        assert "false accepts    1" in text
        assert "Where it disagreed with you" in text
