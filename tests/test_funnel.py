"""
Where the jobs go.

Every number on this page could already be got at by writing a query. What
could not be got at is the shape they make together — and a hundred and fifty
thousand jobs fetched against forty applications sent is either a working
filter or a broken one depending entirely on what happened in between.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.application import Application, ApplicationStatus
from app.models.job import Job, JobStatus
from app.models.job_score import JobScore
from app.services import funnel

NOW = datetime.now(timezone.utc)


def _job(db, **kwargs) -> Job:
    defaults = dict(
        source="greenhouse", source_urls=[f"https://x/{uuid.uuid4()}"],
        title="Backend Engineer", company="Acme",
        url=f"https://x/{uuid.uuid4()}", description="A posting.",
        status=JobStatus.new, fetched_at=NOW, dedupe_hash=uuid.uuid4().hex,
    )
    defaults.update(kwargs)
    job = Job(**defaults)
    db.add(job)
    db.commit()
    return job


class TestTheWholeMachine:
    def test_the_statuses_add_up(self, db):
        _job(db, status=JobStatus.new)
        _job(db, status=JobStatus.filtered_out, filter_reason="low_score")
        _job(db, status=JobStatus.matched)
        _job(db, status=JobStatus.docs_generated)

        result = funnel.overview(db)

        assert result["total"] == 4
        assert result["by_status"]["matched"] == 1
        assert result["by_status"]["docs_generated"] == 1

    def test_the_drop_is_broken_out_by_reason(self, db):
        # The total on its own is uninterpretable: a hundred thousand dropped
        # on a title mismatch is the system working, and the same number
        # dropped for having no description is a fetching problem.
        for reason in ("low_score", "low_score", "no_description"):
            _job(db, status=JobStatus.filtered_out, filter_reason=reason)

        reasons = {row["reason"]: row for row in funnel.overview(db)["filter_reasons"]}

        assert reasons["low_score"]["count"] == 2
        assert reasons["low_score"]["share"] == 66.7
        assert reasons["no_description"]["label"] == "No job description available"

    def test_an_unrecorded_reason_is_labelled_rather_than_blank(self, db):
        _job(db, status=JobStatus.filtered_out, filter_reason=None)
        row = funnel.overview(db)["filter_reasons"][0]
        assert row["reason"] == "unknown"
        assert row["label"] == "Not recorded"

    def test_only_sent_applications_count_as_sent(self, db):
        # Documents written and never sent is silence, not an application.
        job = _job(db, status=JobStatus.docs_generated)
        db.add(Application(job_id=job.id, status=ApplicationStatus.not_applied))
        other = _job(db, status=JobStatus.docs_generated)
        db.add(Application(job_id=other.id, status=ApplicationStatus.applied))
        db.commit()

        result = funnel.overview(db)

        assert result["sent"] == 1
        assert result["applications"]["not_applied"] == 1

    def test_the_ratio_that_describes_everything(self, db):
        for _ in range(9):
            _job(db, status=JobStatus.filtered_out, filter_reason="low_score")
        job = _job(db, status=JobStatus.docs_generated)
        db.add(Application(job_id=job.id, status=ApplicationStatus.applied))
        db.commit()

        assert funnel.overview(db)["sent_per_thousand"] == 100.0

    def test_an_empty_database_does_not_divide_by_zero(self, db):
        result = funnel.overview(db)
        assert result["total"] == 0
        assert result["sent_per_thousand"] == 0.0


class TestByTheDayTheyArrived:
    def test_each_day_reports_what_became_of_its_jobs(self, db):
        _job(db, fetched_at=NOW - timedelta(days=1), status=JobStatus.matched)
        _job(db, fetched_at=NOW - timedelta(days=1),
             status=JobStatus.filtered_out, filter_reason="low_score")
        _job(db, fetched_at=NOW - timedelta(days=2), status=JobStatus.new)

        rows = funnel.cohorts(db, days=7)

        assert len(rows) == 2
        assert rows[0]["fetched"] == 2      # newest first
        assert rows[0]["matched"] == 1
        assert rows[0]["filtered"] == 1
        assert rows[1]["new"] == 1

    def test_days_outside_the_window_are_excluded(self, db):
        _job(db, fetched_at=NOW - timedelta(days=90))
        assert funnel.cohorts(db, days=30) == []


class TestSourceYield:
    def test_the_ratio_is_per_thousand_fetched(self, db, monkeypatch):
        monkeypatch.setattr(
            "app.services.fetch_history.source_totals",
            lambda db, runs: [
                {"source": "adzuna", "runs": 5, "fetched": 2000, "inserted": 40,
                 "merged": 10, "failed_runs": 0},
            ],
        )
        row = funnel.source_roi(db)[0]
        assert row["new_per_thousand"] == 20.0
        assert row["silent"] is False

    def test_a_source_that_returned_nothing_is_named_not_scored(self, db, monkeypatch):
        # Returning nothing and returning plenty while contributing none are
        # different failures with the same ratio.
        monkeypatch.setattr(
            "app.services.fetch_history.source_totals",
            lambda db, runs: [
                {"source": "dead", "runs": 5, "fetched": 0, "inserted": 0,
                 "merged": 0, "failed_runs": 5},
            ],
        )
        row = funnel.source_roi(db)[0]
        assert row["silent"] is True
        assert row["new_per_thousand"] == 0.0


class TestScoreDistribution:
    def test_bands_show_a_model_piling_into_one_answer(self, db):
        # The failure an average cannot show: every score in one band is a
        # model that has found a safe reply, not one that is judging.
        for _ in range(8):
            _job(db, matched_by="nim/glm", llm_score=70)
        for _ in range(2):
            _job(db, matched_by="nim/glm", llm_score=20)

        row = [r for r in funnel.score_distribution(db)
               if r["model"] == "nim/glm"][0]

        assert row["count"] == 10
        bands = {(b["low"], b["high"]): b for b in row["bands"]}
        assert bands[(65, 75)]["count"] == 8
        assert bands[(65, 75)]["share"] == 80.0
        assert bands[(0, 25)]["count"] == 2

    def test_the_two_passes_are_reported_separately(self, db):
        # Keeping both numbers is only worth anything if they can be compared.
        _job(db, matched_by="nim/glm", llm_score=62,
             deep_matched_by="anthropic/opus", llm_score_deep=88)

        passes = {row["pass"]: row for row in funnel.score_distribution(db)}

        assert passes["first pass"]["model"] == "nim/glm"
        assert passes["second look"]["model"] == "anthropic/opus"
        assert passes["second look"]["average"] == 88.0

    def test_unscored_jobs_are_absent(self, db):
        _job(db)
        assert funnel.score_distribution(db) == []


class TestSecondOpinion:
    def test_it_reports_the_shift_and_what_it_changed(self, db):
        # Rescued: the deep pass raised the score and the job is not filtered.
        _job(db, status=JobStatus.matched, llm_score=62, llm_score_deep=88)
        # Dropped: it lowered the score and the job was filtered.
        _job(db, status=JobStatus.filtered_out, filter_reason="low_score",
             llm_score=70, llm_score_deep=40)

        result = funnel.second_opinion(db)

        assert result["rescored"] == 2
        assert result["rescued"] == 1
        assert result["dropped"] == 1
        assert result["avg_shift"] == -2.0

    def test_nothing_rescored_reports_zero_not_none(self, db):
        _job(db, llm_score=62)
        assert funnel.second_opinion(db)["rescored"] == 0


class TestEnrichmentEffect:
    def test_it_counts_verdicts_the_pipeline_changed_its_mind_about(self, db):
        # Read from the score history, because the job holds only the verdict
        # that stuck — the whole point is the one it overturned.
        job = _job(db, status=JobStatus.matched)
        db.add_all([
            JobScore(job_id=job.id, trigger="initial", status="filtered_out",
                     score=45, description_chars=500),
            JobScore(job_id=job.id, trigger="description_grew", status="matched",
                     score=82, description_chars=6200),
        ])
        db.commit()

        result = funnel.enrichment_effect(db)

        assert result["rescored"] == 1
        assert result["now_matched"] == 1
        assert result["avg_chars"] == 6200

    def test_an_empty_history_is_zeroes(self, db):
        assert funnel.enrichment_effect(db) == {
            "rescored": 0, "now_matched": 0, "avg_chars": 0,
        }


class TestThePage:
    def test_it_renders(self, client, db):
        _job(db, status=JobStatus.filtered_out, filter_reason="low_score",
             matched_by="nim/glm", llm_score=30)
        body = client.get("/funnel").text

        assert "Where the jobs go" in body
        assert "AI score below your minimum" in body
        assert "nim/glm" in body

    def test_it_renders_on_an_empty_database(self, client, db):
        # A dashboard is the page you open when something is already wrong, so
        # it has to survive there being nothing at all.
        body = client.get("/funnel").text
        assert client.get("/funnel").status_code == 200
        assert "Nothing has been filtered out yet." in body

    def test_one_broken_panel_does_not_lose_the_page(self, db, client, monkeypatch):
        monkeypatch.setattr(
            "app.services.funnel.score_distribution",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        body = client.get("/funnel")
        assert body.status_code == 200
        assert "Nothing has been scored by a model yet." in body.text

    def test_the_window_is_bounded(self, client, db):
        assert client.get("/funnel?days=99999").status_code == 200
        assert client.get("/funnel?days=-5").status_code == 200

    def test_it_is_linked_from_the_nav(self, client, db):
        assert 'href="/funnel"' in client.get("/jobs").text
