"""
Matching measured against what the user did: dismiss reasons, the agreement
report and its threshold suggestion, the similarity pre-screen, the learned
"For you" ranking, and skill synonyms.
"""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.models.application import Application, ApplicationStatus
from app.models.job import Job, JobStatus
from app.models.profile import Profile
from app.services import for_you, match_report, similarity, tunables

NOW = datetime.now(timezone.utc)


def make_job(db, *, score=None, title="Backend Engineer", company="Acme", remote=False,
             status=JobStatus.matched, description="Python services.", **extra):
    job = Job(source="greenhouse", url=f"https://x/{uuid.uuid4()}", source_urls=[], title=title,
              company=company, description=description, status=status, llm_score=score,
              is_remote=remote, fetched_at=extra.pop("fetched_at", NOW - timedelta(days=1)),
              dedupe_hash=uuid.uuid4().hex, **extra)
    db.add(job)
    db.flush()
    return job


def applied(db, job, when=None):
    db.add(Application(job_id=job.id, status=ApplicationStatus.applied,
                       applied_at=when or NOW - timedelta(days=1)))
    db.flush()


def dismissed(job, reason=None, when=None):
    job.status = JobStatus.filtered_out
    job.filter_reason = "manual"
    job.dismiss_reason = reason
    job.dismissed_at = when or NOW - timedelta(days=1)


def store_profile(db, data=None):
    db.query(Profile).delete()
    db.add(Profile(data=data or {}))
    db.commit()


# ---------------------------------------------------------------------------
# Dismiss reasons (M11)
# ---------------------------------------------------------------------------

class TestDismissReasons:
    def test_a_reason_is_kept_with_the_time(self, client, db):
        job = make_job(db, score=80)
        db.commit()
        client.post(f"/jobs/{job.id}/not-interested", data={"scope": "job", "reason": "too_senior"})
        db.refresh(job)
        assert job.dismiss_reason == "too_senior" and job.filter_reason == "manual"
        assert (NOW - job.dismissed_at).total_seconds() < 60

    def test_excluding_the_company_or_a_title_word_is_its_own_reason(self, client, db):
        store_profile(db)
        a, b = make_job(db, score=80), make_job(db, score=80, title="Full Stack Engineer")
        db.commit()
        client.post(f"/jobs/{a.id}/not-interested", data={"scope": "company"})
        client.post(f"/jobs/{b.id}/not-interested", data={"scope": "title_word", "word": "Stack"})
        db.refresh(a)
        db.refresh(b)
        assert (a.dismiss_reason, b.dismiss_reason) == ("company", "role")

    def test_an_unknown_reason_is_refused(self, client, db):
        job = make_job(db, score=80)
        db.commit()
        reply = client.post(f"/jobs/{job.id}/not-interested", data={"scope": "job", "reason": "meh"})
        assert reply.status_code == 422

    def test_reinstating_a_job_takes_the_no_back(self, client, db):
        job = make_job(db, score=80)
        dismissed(job, "location")
        db.commit()
        client.post(f"/jobs/{job.id}/override")
        db.refresh(job)
        assert job.status == JobStatus.matched and job.dismiss_reason is None and job.dismissed_at is None

    def test_the_card_offers_the_reasons(self, client, db):
        job = make_job(db, score=80)
        db.commit()
        page = client.get("/jobs").text
        assert '"reason": "too_senior"' in page and "Wrong location" in page


# ---------------------------------------------------------------------------
# The report (M7)
# ---------------------------------------------------------------------------

def decided(db, yes_scores, no_scores, reason="stack"):
    for score in yes_scores:
        applied(db, make_job(db, score=score))
    for score in no_scores:
        dismissed(make_job(db, score=score), reason)
    db.commit()


class TestDecisions:
    def test_what_counts_as_a_yes_and_a_no(self, db):
        yes = make_job(db, score=80)
        applied(db, yes)
        starred = make_job(db, score=75, favourite=True, favourited_at=NOW)
        no = make_job(db, score=85)
        dismissed(no, "pay")
        by_rule = make_job(db, score=90, status=JobStatus.filtered_out,
                           filter_reason="excluded_company")
        make_job(db, score=90)  # no decision at all
        db.commit()
        rows = {r["id"]: r for r in match_report.decisions(db)}
        assert rows[yes.id]["verdict"] == "yes" and rows[starred.id]["verdict"] == "yes"
        assert rows[no.id]["verdict"] == "no" and rows[no.id]["why"] == "pay"
        assert by_rule.id not in rows and len(rows) == 3

    def test_applied_then_dismissed_is_still_a_yes(self, db):
        job = make_job(db, score=80)
        applied(db, job)
        dismissed(job, "other")
        db.commit()
        assert [r["verdict"] for r in match_report.decisions(db)] == ["yes"]


class TestAgreement:
    def test_counted_at_the_current_threshold(self, db):
        decided(db, [90, 80, 60], [85, 40])
        report = match_report.build(db, {tunables.STORE_KEY: {"min_match_score": 70}})
        a = report["all"]
        assert (a["yes"], a["yes_kept"], a["no"], a["no_shown"]) == (3, 2, 2, 1)
        assert a["agreed"] == 3 and a["missed"][0]["score"] == 60
        assert report["week"]["yes"] == 3

    def test_last_week_is_its_own_count(self, db):
        old = make_job(db, score=90)
        applied(db, old, when=NOW - timedelta(days=30))
        decided(db, [80], [])
        report = match_report.build(db, {})
        assert report["all"]["yes"] == 2 and report["week"]["yes"] == 1


class TestTheSuggestion:
    def test_too_few_decisions_says_how_many_more(self, db):
        decided(db, [80] * 3, [50])
        s = match_report.build(db, {})["suggestion"]
        assert s["kind"] == "wait" and s["need_yes"] == 5 and s["need_no"] == 4

    def test_a_threshold_well_under_every_wanted_job_is_raised(self, db):
        decided(db, [85, 88, 90, 92, 95, 86, 87, 91, 89, 93], [55, 60, 62, 65, 70, 72])
        s = match_report.build(db, {tunables.STORE_KEY: {"min_match_score": 50}})["suggestion"]
        assert s["kind"] == "raise" and s["threshold"] == 85
        assert s["row"]["no_hidden"] == 6

    def test_a_threshold_that_loses_wanted_jobs_is_lowered(self, db):
        decided(db, [55, 58, 62, 64, 66, 68, 75, 80, 85, 90], [30, 35, 40, 42, 45])
        s = match_report.build(db, {tunables.STORE_KEY: {"min_match_score": 70}})["suggestion"]
        assert s["kind"] == "lower" and s["threshold"] == 55
        assert round(1 - s["current"]["yes_share"], 1) == 0.6

    def test_a_threshold_that_fits_is_left(self, db):
        decided(db, [72, 75, 80, 85, 90, 78, 82, 88, 76, 74], [40, 45, 50, 55, 60])
        s = match_report.build(db, {tunables.STORE_KEY: {"min_match_score": 70}})["suggestion"]
        assert s["kind"] == "keep"

    def test_reasons_are_counted_with_where_to_fix_them(self, db):
        decided(db, [], [50, 60], reason="too_senior")
        decided(db, [], [70], reason="location")
        reasons = match_report.build(db, {})["reasons"]
        assert [(r["key"], r["count"]) for r in reasons] == [("too_senior", 2), ("location", 1)]
        assert "junior threshold" in reasons[0]["hint"]


class TestThePage:
    def test_it_renders_the_report(self, client, db):
        store_profile(db, {tunables.STORE_KEY: {"min_match_score": 50}})
        decided(db, [85, 88, 90, 92, 95, 86, 87, 91, 89, 93], [55, 60, 62, 65, 70, 72], "pay")
        page = client.get("/funnel/matching").text
        assert "agreement on 16 scored decisions" in page
        assert "Use 85" in page and "pay too low" in page

    def test_using_the_suggestion_sets_the_threshold_matching_files_against(self, client, db):
        store_profile(db, {tunables.STORE_KEY: {"min_match_score": 50}})
        reply = client.post("/funnel/matching/threshold", data={"value": "85"},
                            follow_redirects=False)
        assert reply.status_code == 303
        db.expire_all()
        data = db.query(Profile).first().data
        # The same override the settings page writes, legacy key included, so
        # the matcher and the skills tab both read 85.
        assert tunables.value(data, "min_match_score") == 85
        assert data["min_match_score"] == 85

    def test_the_funnel_links_to_it(self, client, db):
        assert "/funnel/matching" in client.get("/funnel").text

    def test_the_weekly_line_goes_to_the_log(self, db, caplog):
        from app.tasks.match_eval import report_matching

        store_profile(db)
        decided(db, [80], [40])
        with patch("app.tasks.match_eval.SessionLocal", return_value=db), \
                patch.object(db, "close"), caplog.at_level("INFO"):
            result = report_matching()
        assert "agreed with 2 of your 2 scored decisions" in result["summary"]
        assert "matching report:" in caplog.text


# ---------------------------------------------------------------------------
# The similarity pre-screen (M8)
# ---------------------------------------------------------------------------

PROFILE = {
    "target_roles": ["Data Engineer"],
    "narrative": {"summary": "Data engineer building Kafka and Spark pipelines."},
    "skills": {"data": ["Kafka", "Spark", "Airflow", "Python"]},
    "experience": [{"role": "Data Engineer", "company": "Initech",
                    "bullets": ["Built Kafka ingestion into a Spark lakehouse with Airflow"]}],
}

CORPUS = [
    ("Data Engineer", "Kafka Spark Airflow pipelines for the lakehouse, Python."),
    ("Nurse", "Patient care on the ward, shifts, clinical charting."),
    ("Sales Manager", "Quota, pipeline of accounts, travel to clients."),
    ("Accountant", "Ledgers, audits, month end close, reconciliations."),
    ("Frontend Engineer", "React, CSS, accessibility, design systems."),
]


@pytest.fixture
def corpus(db):
    similarity.reset_cache()
    for title, text in CORPUS:
        make_job(db, title=title, description=text)
    db.commit()
    yield
    similarity.reset_cache()


class TestSimilarity:
    def test_a_posting_about_the_work_scores_above_one_that_is_not(self, db, corpus):
        scorer = similarity.scorer(db, PROFILE)
        fit = SimpleNamespace(title="Data Engineer", description="Kafka and Spark streaming, Airflow.")
        other = SimpleNamespace(title="Registered Nurse", description="Clinical shifts on the ward.")
        assert scorer.score(fit) > 50 > scorer.score(other)

    def test_with_no_corpus_it_says_nothing(self, db):
        similarity.reset_cache()
        scorer = similarity.Scorer(PROFILE, {}, 0)
        assert scorer.score(SimpleNamespace(title="x", description="y")) is None

    def screen(self, db, overrides, job):
        from app.services.matcher import _screen

        store_profile(db, {tunables.STORE_KEY: overrides})
        profile = {**PROFILE, "target_roles": ["Data Engineer", "Nurse"], "skills": {}}
        return _screen(job, profile, similarity.scorer(db, PROFILE))

    def test_off_by_default_it_only_measures(self, db, corpus):
        job = make_job(db, title="Registered Nurse", description="Clinical shifts on the ward.")
        assert self.screen(db, {}, job) is None
        assert job.similarity is not None and job.similarity < 30

    def test_set_on_the_settings_page_it_skips_the_model_call(self, db, corpus):
        job = make_job(db, title="Registered Nurse", description="Clinical shifts on the ward.")
        assert self.screen(db, {"prescreen_min_similarity": 40}, job) == "filtered_out"
        assert job.filter_reason == "low_similarity" and "not sent to the model" in job.filter_detail

    def test_a_similar_job_passes_the_same_threshold(self, db, corpus):
        job = make_job(db, title="Data Engineer", description="Kafka Spark Airflow pipelines.")
        assert self.screen(db, {"prescreen_min_similarity": 40}, job) is None

    def test_the_report_sweeps_it_against_what_you_applied_to(self, db, corpus):
        wanted = make_job(db, score=80, similarity=15)
        applied(db, wanted)
        make_job(db, score=40, similarity=8)
        make_job(db, score=60, similarity=35)
        db.commit()
        sweep = {row["threshold"]: row for row in match_report.build(db, {})["similarity"]}
        assert sweep[10]["calls_saved"] == 1 and sweep[10]["yes_lost"] == 0
        assert sweep[20]["calls_saved"] == 2 and sweep[20]["yes_lost"] == 1

    def test_a_pre_screened_job_is_revisited_when_its_description_grows(self):
        from app.services.matcher import DESCRIPTION_DEPENDENT_REASONS

        assert "low_similarity" in DESCRIPTION_DEPENDENT_REASONS


# ---------------------------------------------------------------------------
# For you (M9)
# ---------------------------------------------------------------------------

def remote_lover(db, n=14):
    """Applies to remote jobs whatever they score; dismisses on-site ones."""
    for i in range(n):
        applied(db, make_job(db, score=60 + i % 10, remote=True, title=f"Platform Engineer {i}"))
        dismissed(make_job(db, score=80 + i % 10, remote=False, title=f"Onsite Engineer {i}"),
                  "location")
    db.commit()


class TestForYou:
    def test_too_few_decisions_are_not_a_model(self, db):
        decided(db, [80] * 3, [50] * 3)
        model = for_you.fit(db, {})
        assert model["usable"] is False and "needs 10 of each" in model["reason"]

    def test_it_learns_what_the_score_misses_and_says_so(self, db):
        remote_lover(db)
        model = for_you.fit(db, {})
        assert model["usable"] is True
        assert model["auc"] > model["score_auc"]
        assert model["weights"]["remote"] > 0

    def test_it_ranks_by_what_you_did(self, db):
        remote_lover(db)
        model = for_you.fit(db, {})
        remote = SimpleNamespace(title="Platform Engineer", llm_score=65, llm_score_deep=None,
                                 is_remote=True, company="Acme", source="greenhouse")
        onsite = SimpleNamespace(title="Onsite Engineer", llm_score=90, llm_score_deep=None,
                                 is_remote=False, company="Acme", source="greenhouse")
        assert [job for _, job in for_you.rank(model, [onsite, remote])] == [remote, onsite]

    def test_the_jobs_list_sorts_by_it_once_it_is_trained(self, client, db):
        store_profile(db)
        remote_lover(db)
        assert 'value="for_you"' not in client.get("/jobs").text
        client.post("/funnel/matching/retrain")
        top = make_job(db, score=55, remote=True, title="Remote Platform Engineer")
        bottom = make_job(db, score=95, remote=False, title="Onsite Engineer X")
        db.commit()
        page = client.get("/jobs?sort=for_you&status=matched").text
        assert 'value="for_you" selected' in page
        assert page.index(f'id="job-{top.id}"') < page.index(f'id="job-{bottom.id}"')

    def test_an_untrained_sort_falls_back_to_the_score(self, client, db):
        store_profile(db)
        page = client.get("/jobs?sort=for_you").text
        assert 'value="score_desc" selected' in page

    def test_the_schedule_retrains_it(self, db):
        from app.tasks.match_eval import retrain_for_you

        store_profile(db)
        remote_lover(db)
        with patch("app.tasks.match_eval.SessionLocal", return_value=db), patch.object(db, "close"):
            result = retrain_for_you()
        assert result["usable"] is True
        db.expire_all()
        assert db.query(Profile).first().data[for_you.STORE_KEY]["usable"] is True


class TestTheirIntervals:
    @pytest.mark.parametrize("name, key", [("report-matching", "match_report_interval_hours"),
                                           ("retrain-for-you", "for_you_retrain_hours")])
    def test_each_runs_on_the_interval_the_settings_page_sets(self, name, key):
        from app.tasks.schedule import HOUR, SCHEDULE, interval_seconds

        entry = next(e for e in SCHEDULE if e.name == name)
        assert interval_seconds(entry, {tunables.STORE_KEY: {key: 6}}) == 6 * HOUR
        assert interval_seconds(entry, {tunables.STORE_KEY: {key: 48}}) == 48 * HOUR


# ---------------------------------------------------------------------------
# Skill synonyms (M10)
# ---------------------------------------------------------------------------

class TestSkillSynonyms:
    def test_lines_are_read_as_groups(self):
        from app.services.matcher import parse_alias_lines

        assert parse_alias_lines("KStreams = Kafka Streams\n\nsolo\nGCP, GCloud") == [
            ["KStreams", "Kafka Streams"], ["GCP", "GCloud"]]

    def test_your_groups_join_the_built_in_ones(self):
        from app.services.matcher import alias_index

        index = alias_index({"skill_aliases": [["GCP", "GCloud"], ["KStreams", "Kafka Streams"]]})
        assert index["gcloud"] >= {"gcp", "google cloud", "gcloud"}
        assert index["kafka streams"] == frozenset({"kstreams", "kafka streams"})

    def test_the_keyword_filter_counts_your_names(self):
        from app.services.matcher import _count_skill_matches, alias_index

        desc = "We use KStreams and GCloud."
        skills = ["Kafka Streams", "GCP"]
        assert _count_skill_matches(desc, skills) == 0
        aliases = alias_index({"skill_aliases": [["GCP", "GCloud"], ["KStreams", "Kafka Streams"]]})
        assert _count_skill_matches(desc, skills, aliases) == 2

    def test_the_invented_content_check_counts_them_too(self):
        from app.services import content_checks

        profile = {"skills": {"data": ["Kafka Streams"]},
                   "experience": [{"id": "e", "company": "Initech", "role": "Engineer",
                                   "bullets": ["Built KStreams jobs"]}]}
        ctx = {"experience": [{"id": "e", "company": "Initech", "title": "Engineer",
                               "bullets": ["Built Kafka Streams jobs for billing"]}]}
        assert content_checks.check_resume(ctx, profile) != []
        profile["skill_aliases"] = [["KStreams", "Kafka Streams"]]
        assert content_checks.check_resume(ctx, profile) == []

    def test_a_keyword_written_another_way_is_named(self):
        from app.services import document_content, pdf_text

        with patch.object(pdf_text, "extract", return_value="Ran Postgres and Python"):
            ats = document_content.ats_check("/x.pdf", ["PostgreSQL", "Python", "Rust"], {})
        assert ats["missing"] == ["PostgreSQL", "Rust"]
        assert ats["written_as"] == {"PostgreSQL": "postgres"}

    def test_the_skills_tab_saves_and_shows_them(self, client, db):
        store_profile(db, {"skills": {}})
        client.post("/profile/skills", data={"skill_aliases": "KStreams = Kafka Streams",
                                             "min_match_score": "70"})
        db.expire_all()
        assert db.query(Profile).first().data["skill_aliases"] == [["KStreams", "Kafka Streams"]]
        assert "KStreams = Kafka Streams" in client.get("/profile?tab=skills").text
