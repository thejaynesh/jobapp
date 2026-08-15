"""
The stalls that leave no error behind.

Two things could stop with nothing recorded anywhere: a matching pass whose
worker was killed (Celery acked on receipt, so the task simply ceased to exist),
and the document generations it would have queued only after finishing the
whole pass. Both looked identical to "working on it" from every page in the app,
which is what these tests are really about — a stage that has stopped must be
distinguishable from a stage that is slow, without a container shell.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.config import settings
from app.models.application import Application, ApplicationDocument, DocType
from app.models.job import Job, JobStatus

NOW = datetime.now(timezone.utc)


def make_job(db, suffix="1", status=JobStatus.new, **kwargs):
    job = Job(
        source="adzuna",
        title=kwargs.pop("title", "Backend Engineer"),
        company=kwargs.pop("company", "Acme"),
        location="Boston, MA",
        url=f"https://example.com/job/{suffix}",
        description="A job.",
        dedupe_hash=f"hash-{suffix}",
        fetched_at=kwargs.pop("fetched_at", NOW),
        status=status,
        **kwargs,
    )
    db.add(job)
    db.flush()
    return job


def make_application(db, job=None, suffix="1", generation_status="idle", **kwargs):
    job = job or make_job(db, suffix, status=JobStatus.matched)
    app = Application(job_id=job.id, generation_status=generation_status, **kwargs)
    db.add(app)
    db.flush()
    return app


@pytest.fixture
def no_lock(monkeypatch):
    """Redis is not up in tests; the lock's own fallback is tested separately."""
    import app.tasks.match as match_task

    monkeypatch.setattr(match_task, "acquire", lambda **kwargs: True)
    monkeypatch.setattr(match_task, "release", lambda **kwargs: None)


# ---------------------------------------------------------------------------
# Celery configuration
# ---------------------------------------------------------------------------

class TestTasksSurviveARestart:
    def test_acks_happen_after_the_work_not_before(self):
        # With the default (ack on receipt), a worker killed mid-task takes the
        # task with it: nothing errors, nothing retries, the work simply never
        # happened. That is how a deploy could silently cancel a matching pass.
        from app.celery_app import celery_app

        assert celery_app.conf.task_acks_late is True

    def test_a_lost_worker_puts_its_task_back(self):
        from app.celery_app import celery_app

        assert celery_app.conf.task_reject_on_worker_lost is True


class TestMatchingHasItsOwnSchedule:
    def test_matching_does_not_depend_on_a_fetch_happening(self):
        # It used to run only as a tail-call from fetch_jobs, so anything one
        # pass did not finish waited hours for the next fetch — indistinguishable
        # from matching being broken.
        from app.celery_app import celery_app

        entry = celery_app.conf.beat_schedule["match-new-jobs"]
        assert entry["task"] == "app.tasks.match.match_jobs"
        assert entry["schedule"].seconds == settings.MATCH_INTERVAL_MINUTES * 60

    def test_the_sweep_is_scheduled_too(self):
        from app.celery_app import celery_app

        entry = celery_app.conf.beat_schedule["sweep-stuck-generations"]
        assert entry["task"] == "app.tasks.generate.sweep_generations"

    def test_the_sweep_threshold_clears_a_generations_own_time_limit(self):
        # Below it, the sweeper would re-queue runs that are merely still going.
        from app.tasks.generate import generate_docs

        assert settings.GENERATION_STUCK_MINUTES * 60 > generate_docs.time_limit


# ---------------------------------------------------------------------------
# Bounded matching
# ---------------------------------------------------------------------------

class TestBoundedPasses:
    def _patch_match(self, results):
        """Feed match_job a scripted sequence of outcomes."""
        return patch("app.services.matcher.match_job", side_effect=results)

    def test_a_pass_scores_at_most_its_limit(self, db):
        from app.services.matcher import match_all_new_jobs

        for i in range(5):
            make_job(db, f"lim{i}")
        db.commit()
        with self._patch_match(["filtered_out"] * 5):
            result = match_all_new_jobs(db, limit=2)
        assert result["processed"] == 2

    def test_it_reports_what_is_still_waiting(self, db):
        # The caller's cue to come back, and the number the panel shows.
        from app.services.matcher import match_all_new_jobs

        for i in range(4):
            make_job(db, f"rem{i}")
        db.commit()

        def _score(db_, job, *args, **kwargs):
            job.status = JobStatus.filtered_out
            return "filtered_out"

        with patch("app.services.matcher.match_job", side_effect=_score):
            result = match_all_new_jobs(db, limit=3)
        assert result["remaining"] == 1

    def test_documents_are_queued_as_each_job_matches(self, db):
        # Not after the pass. Fanning out at the end means the first match waits
        # on the hundredth, and a pass that never finishes queues nothing at all.
        from app.services.matcher import match_all_new_jobs

        make_job(db, "live")
        db.commit()
        seen = []

        def _score(db_, job, *args, **kwargs):
            job.status = JobStatus.matched
            db_.add(Application(job_id=job.id))
            return "matched"

        with patch("app.services.matcher.match_job", side_effect=_score):
            match_all_new_jobs(db, on_matched=lambda job: seen.append(job.id))
        assert len(seen) == 1

    def test_a_queueing_failure_does_not_stop_the_pass(self, db):
        from app.services.matcher import match_all_new_jobs

        for i in range(2):
            make_job(db, f"qf{i}")
        db.commit()

        def _score(db_, job, *args, **kwargs):
            job.status = JobStatus.matched
            return "matched"

        def _explode(job):
            raise RuntimeError("broker down")

        with patch("app.services.matcher.match_job", side_effect=_score):
            result = match_all_new_jobs(db, on_matched=_explode)
        assert result["matched"] == 2


class TestMatchTask:
    def test_one_job_queues_its_generation(self, db, no_lock, monkeypatch):
        import app.tasks.match as match_task
        from app.tasks.match import match_jobs

        job = make_job(db, "task1")
        db.commit()
        queued = []

        def _score(db_, j, *args, **kwargs):
            j.status = JobStatus.matched
            db_.add(Application(job_id=j.id))
            return "matched"

        monkeypatch.setattr(match_task, "SessionLocal", lambda: db)
        monkeypatch.setattr("app.tasks.generate.queue_generation",
                            lambda app_id: queued.append(app_id) or True)
        with patch("app.services.matcher.match_job", side_effect=_score):
            with patch.object(match_jobs, "delay"):
                result = match_jobs.apply(kwargs={"limit": 5}).result

        assert result["matched"] == 1
        assert result["queued_for_generation"] == 1

    def test_it_does_not_requeue_an_application_already_being_written(
        self, db, no_lock, monkeypatch
    ):
        import app.tasks.match as match_task
        from app.tasks.match import match_jobs

        job = make_job(db, "task2")
        make_application(db, job, generation_status="generating")
        db.commit()

        def _score(db_, j, *args, **kwargs):
            j.status = JobStatus.matched
            return "matched"

        queued = []
        monkeypatch.setattr(match_task, "SessionLocal", lambda: db)
        monkeypatch.setattr("app.tasks.generate.queue_generation",
                            lambda app_id: queued.append(app_id) or True)
        with patch("app.services.matcher.match_job", side_effect=_score):
            with patch.object(match_jobs, "delay"):
                match_jobs.apply().result
        assert queued == []

    def test_more_to_do_means_another_batch(self, db, no_lock, monkeypatch):
        import app.tasks.match as match_task
        from app.tasks.match import match_jobs

        for i in range(3):
            make_job(db, f"chain{i}")
        db.commit()

        def _score(db_, j, *args, **kwargs):
            j.status = JobStatus.filtered_out
            return "filtered_out"

        monkeypatch.setattr(match_task, "SessionLocal", lambda: db)
        with patch("app.services.matcher.match_job", side_effect=_score):
            with patch.object(match_jobs, "delay") as delay:
                match_jobs.apply(kwargs={"limit": 1})
        assert delay.called

    def test_nothing_left_means_no_follow_up(self, db, no_lock, monkeypatch):
        import app.tasks.match as match_task
        from app.tasks.match import match_jobs

        make_job(db, "last")
        db.commit()

        def _score(db_, j, *args, **kwargs):
            j.status = JobStatus.filtered_out
            return "filtered_out"

        monkeypatch.setattr(match_task, "SessionLocal", lambda: db)
        with patch("app.services.matcher.match_job", side_effect=_score):
            with patch.object(match_jobs, "delay") as delay:
                match_jobs.apply(kwargs={"limit": 10})
        assert not delay.called

    def test_a_batch_that_got_nowhere_stops_instead_of_spinning(
        self, db, no_lock, monkeypatch
    ):
        # Every job rate-limited means the provider is refusing calls and each
        # job stays `new`. Chaining there hammers a wall; the schedule retries.
        import app.tasks.match as match_task
        from app.tasks.match import match_jobs

        for i in range(3):
            make_job(db, f"rl{i}")
        db.commit()

        monkeypatch.setattr(match_task, "SessionLocal", lambda: db)
        with patch("app.services.matcher.match_job", return_value="rate_limited"):
            with patch.object(match_jobs, "delay") as delay:
                match_jobs.apply(kwargs={"limit": 2})
        assert not delay.called

    def test_two_passes_do_not_overlap(self, db, monkeypatch):
        # Overlapping passes score the same jobs twice and double the LLM spend.
        import app.tasks.match as match_task
        from app.tasks.match import match_jobs

        monkeypatch.setattr(match_task, "acquire", lambda **kwargs: False)
        monkeypatch.setattr(match_task, "release", lambda **kwargs: None)
        result = match_jobs.apply().result
        assert result["skipped_reason"] == "already running"

    def test_the_lock_is_released_even_when_the_pass_blows_up(self, db, monkeypatch):
        import app.tasks.match as match_task
        from app.tasks.match import match_jobs

        released = []
        monkeypatch.setattr(match_task, "acquire", lambda **kwargs: True)
        monkeypatch.setattr(match_task, "release",
                            lambda **kwargs: released.append(kwargs.get("key")))
        monkeypatch.setattr(match_task, "match_all_new_jobs",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        monkeypatch.setattr(match_task, "SessionLocal", lambda: db)
        match_jobs.apply()
        assert released == [match_task.MATCH_LOCK_KEY]

    def test_it_does_not_share_the_fetch_lock(self):
        # Sharing one key would have a fetch and a matching pass block each
        # other for no reason — they do not conflict.
        from app.services.fetch_lock import LOCK_KEY
        from app.tasks.match import MATCH_LOCK_KEY

        assert MATCH_LOCK_KEY != LOCK_KEY


# ---------------------------------------------------------------------------
# The generation sweeper
# ---------------------------------------------------------------------------

class TestSweepGenerations:
    def _run(self, db, monkeypatch):
        import app.tasks.generate as generate_task
        from app.tasks.generate import sweep_generations

        queued = []
        monkeypatch.setattr(generate_task, "SessionLocal", lambda: db)
        monkeypatch.setattr(generate_task, "queue_generation",
                            lambda app_id: queued.append(str(app_id)) or True)
        result = sweep_generations.apply().result
        return result, queued

    def test_a_generation_whose_worker_died_is_picked_back_up(self, db, monkeypatch):
        app = make_application(
            db, suffix="stale", generation_status="generating",
            generation_started_at=NOW - timedelta(hours=3),
        )
        db.commit()
        result, queued = self._run(db, monkeypatch)
        assert str(app.id) in queued
        assert result["stalled"] == 1

    def test_one_that_started_a_moment_ago_is_left_alone(self, db, monkeypatch):
        make_application(
            db, suffix="fresh", generation_status="generating",
            generation_started_at=NOW,
        )
        db.commit()
        result, queued = self._run(db, monkeypatch)
        assert queued == []

    def test_a_matched_job_whose_generation_was_never_queued_gets_queued(
        self, db, monkeypatch
    ):
        app = make_application(db, suffix="missed", generation_status="idle")
        db.commit()
        result, queued = self._run(db, monkeypatch)
        assert str(app.id) in queued
        assert result["never_queued"] == 1

    def test_a_failure_is_not_retried_on_a_timer(self, db, monkeypatch):
        # It has an error the user can read and a Rewrite button. Re-queueing it
        # every twenty minutes would just spend LLM calls on the same failure.
        make_application(db, suffix="failed", generation_status="failed",
                         generation_error="model refused")
        db.commit()
        result, queued = self._run(db, monkeypatch)
        assert queued == []

    def test_an_application_that_already_has_documents_is_left_alone(
        self, db, monkeypatch
    ):
        app = make_application(db, suffix="hasdocs", generation_status="idle")
        db.add(ApplicationDocument(
            application_id=app.id, doc_type=DocType.resume,
            path="/storage/x.pdf", is_current=True,
        ))
        db.commit()
        result, queued = self._run(db, monkeypatch)
        assert queued == []

    def test_an_unmatched_job_is_not_swept_into_generating(self, db, monkeypatch):
        job = make_job(db, "filtered", status=JobStatus.filtered_out)
        make_application(db, job, generation_status="idle")
        db.commit()
        result, queued = self._run(db, monkeypatch)
        assert queued == []


class TestGenerationRecordsWhenItStarted:
    def test_the_task_stamps_a_start_time(self, db, monkeypatch):
        # Without it, 'generating' is a state with no clock on it, and thirty
        # seconds in looks the same as three days abandoned.
        import app.tasks.generate as generate_task
        from app.tasks.generate import generate_docs

        app = make_application(db, suffix="clock")
        app_id = app.id
        db.commit()
        monkeypatch.setattr(generate_task, "SessionLocal", lambda: db)
        monkeypatch.setattr("app.services.doc_generator.generate_documents",
                            lambda *a, **k: None)
        generate_docs.apply(args=[str(app_id)])
        # The task closes the session it was handed, so re-read rather than
        # refreshing the now-detached instance.
        stored = db.query(Application).filter(Application.id == app_id).one()
        assert stored.generation_started_at is not None


# ---------------------------------------------------------------------------
# What the page shows
# ---------------------------------------------------------------------------

class TestPipelineStatus:
    def test_it_counts_jobs_by_stage(self, db):
        from app.services import pipeline

        make_job(db, "p1", status=JobStatus.new)
        make_job(db, "p2", status=JobStatus.new)
        make_job(db, "p3", status=JobStatus.matched)
        db.commit()
        status = pipeline.status(db)
        stages = {s["key"]: s["count"] for s in status["jobs"]}
        assert stages["new"] == 2
        assert stages["matched"] == 1

    def test_a_stage_at_zero_is_still_listed(self, db):
        # The zero is the number worth noticing; omitting it is how it goes
        # unnoticed.
        from app.services import pipeline

        keys = {s["key"] for s in pipeline.status(db)["jobs"]}
        assert {"new", "matched", "docs_generated", "filtered_out"} <= keys

    def test_it_counts_generations_by_state(self, db):
        from app.services import pipeline

        make_application(db, suffix="g1", generation_status="done")
        make_application(db, suffix="g2", generation_status="failed")
        db.commit()
        stages = {s["key"]: s["count"] for s in pipeline.status(db)["generation"]}
        assert stages["done"] == 1
        assert stages["failed"] == 1

    def test_it_reports_how_long_the_oldest_has_waited(self, db):
        # A count alone cannot tell "slow" from "dead". An age can.
        from app.services import pipeline

        make_job(db, "old", status=JobStatus.new,
                 fetched_at=NOW - timedelta(hours=2))
        db.commit()
        assert pipeline.status(db)["oldest_unmatched_minutes"] >= 119

    def test_a_generation_past_the_threshold_is_called_stalled(self, db):
        from app.services import pipeline

        make_application(
            db, suffix="stall", generation_status="generating",
            generation_started_at=NOW - timedelta(hours=5),
        )
        db.commit()
        assert pipeline.status(db)["generation_stalled"] is True

    def test_one_still_within_it_is_not(self, db):
        from app.services import pipeline

        make_application(db, suffix="running", generation_status="generating",
                         generation_started_at=NOW)
        db.commit()
        assert pipeline.status(db)["generation_stalled"] is False

    def test_the_reason_a_generation_failed_comes_with_it(self, db):
        # A failure whose reason lives only in a worker log is one nobody acts on.
        from app.services import pipeline

        make_application(db, suffix="why", generation_status="failed",
                         generation_error="NIM returned 429 for 6 minutes")
        db.commit()
        failures = pipeline.status(db)["failures"]
        assert "429" in failures[0]["error"]
        assert failures[0]["company"] == "Acme"

    def test_an_unreachable_queue_says_so_rather_than_reporting_zero(self, monkeypatch):
        # Zero waiting and "cannot see the queue" mean opposite things.
        from app.services import pipeline

        monkeypatch.setattr(settings, "REDIS_URL", "redis://127.0.0.1:1/0")
        depth = pipeline.queue_depth()
        assert depth["reachable"] is False
        assert depth["waiting"] is None


class TestPipelinePanel:
    def test_the_runs_page_shows_it(self, client):
        assert "Pipeline" in client.get("/runs").text

    def test_it_renders_on_an_empty_database(self, client):
        assert client.get("/runs/system").status_code == 200

    def test_waiting_jobs_are_visible_without_a_shell(self, client, db):
        make_job(db, "visible", status=JobStatus.new)
        db.commit()
        body = client.get("/runs/system").text
        assert "Waiting to be scored" in body

    def test_a_generation_failure_is_readable_from_the_page(self, client, db):
        make_application(db, suffix="panelfail", generation_status="failed",
                         generation_error="model refused the prompt")
        db.commit()
        assert "model refused the prompt" in client.get("/runs/system").text

    def test_a_pipeline_failure_does_not_take_the_page_down(self, client, monkeypatch):
        from app.services import pipeline

        monkeypatch.setattr(pipeline, "status",
                            lambda db: (_ for _ in ()).throw(RuntimeError("no db")))
        response = client.get("/runs")
        assert response.status_code == 200
        assert "pipeline" in response.text

    def test_the_button_queues_a_batch(self, client):
        with patch("app.tasks.match.match_jobs.delay") as delay:
            response = client.post("/runs/match")
        assert response.status_code == 200
        assert delay.called

    def test_a_broker_that_is_down_does_not_break_the_button(self, client):
        with patch("app.tasks.match.match_jobs.delay",
                   side_effect=RuntimeError("redis down")):
            assert client.post("/runs/match").status_code == 200
