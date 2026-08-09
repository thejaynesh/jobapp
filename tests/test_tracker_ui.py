import uuid
from datetime import datetime, timezone

import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from fastapi import FastAPI

from app.models.job import JobStatus


def _make_job_model(**overrides):
    """
    A real (unsaved) Job rather than a MagicMock, for templates that render one.

    The application detail page reads a dozen job fields and does real work with
    them — `llm_score >= 75`, `posted_at.strftime(...)`, `status.value`. On a
    MagicMock each of those is a Mock that raises or renders as garbage, and
    stubbing them one at a time only defers the breakage to the next field
    someone adds to the template.
    """
    from app.models.job import Job
    fields = {
        "id": uuid.uuid4(),
        "source": "linkedin",
        "source_job_id": "J1",
        "source_urls": ["https://example.com"],
        "title": "Backend Engineer",
        "company": "Acme",
        "location": "New York, NY",
        "is_remote": False,
        "url": "https://example.com",
        "apply_url": None,
        "description": "Build things.",
        "experience_level": "mid",
        "keyword_score": None,
        "llm_score": None,
        "llm_reasoning": None,
        "matched_by": None,
        "matched_skills": [],
        "missing_skills": [],
        "status": JobStatus.matched,
        "fetched_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
        "posted_at": None,
        "dedupe_hash": "h1",
    }
    fields.update(overrides)
    return Job(**fields)


def _make_job(status=JobStatus.matched, title="Backend Engineer", company="Acme"):
    job = MagicMock()
    job.id = uuid.uuid4()
    job.title = title
    job.company = company
    job.location = "Remote"
    job.is_remote = True
    job.url = "https://example.com/job"
    job.status = status
    job.llm_score = 85
    job.keyword_score = 0.8
    job.llm_reasoning = "Strong fit."
    job.matched_skills = ["Python", "FastAPI"]
    job.missing_skills = ["Rust"]
    job.source = "adzuna"
    job.fetched_at = None
    return job


# ---------------------------------------------------------------------------
# Jobs router tests
# ---------------------------------------------------------------------------

class TestJobsRouter:
    def _make_client(self, mock_db):
        from app.routers.jobs import router
        from app.database import get_db
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = lambda: mock_db
        return TestClient(app)

    @staticmethod
    def _mock_jobs_query(mock_db, jobs):
        """Self-chaining query mock supporting filter/count/order_by/offset/limit."""
        query = MagicMock()
        query.filter.return_value = query
        query.count.return_value = len(jobs)
        query.order_by.return_value.offset.return_value.limit.return_value.all.return_value = jobs
        mock_db.query.return_value = query

    def test_get_jobs_returns_200(self):
        mock_db = MagicMock()
        self._mock_jobs_query(mock_db, [_make_job()])
        client = self._make_client(mock_db)
        response = client.get("/jobs")
        assert response.status_code == 200

    def test_get_jobs_html_contains_job_title(self):
        mock_db = MagicMock()
        self._mock_jobs_query(mock_db, [_make_job()])
        client = self._make_client(mock_db)
        response = client.get("/jobs")
        assert "Backend Engineer" in response.text

    def test_get_jobs_filters_by_status(self):
        mock_db = MagicMock()
        self._mock_jobs_query(mock_db, [])
        client = self._make_client(mock_db)
        response = client.get("/jobs?status=matched")
        assert response.status_code == 200

    def test_override_matched_to_filtered(self):
        job = _make_job(status=JobStatus.matched)
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = job
        client = self._make_client(mock_db)
        response = client.post(f"/jobs/{job.id}/override")
        assert response.status_code == 200
        assert job.status == JobStatus.filtered_out

    def test_override_filtered_to_matched(self):
        job = _make_job(status=JobStatus.filtered_out)
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = job
        client = self._make_client(mock_db)
        response = client.post(f"/jobs/{job.id}/override")
        assert response.status_code == 200
        assert job.status == JobStatus.matched

    def test_override_returns_404_for_missing_job(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None
        client = self._make_client(mock_db)
        response = client.post(f"/jobs/{uuid.uuid4()}/override")
        assert response.status_code == 404

    def test_get_jobs_no_jobs_shows_empty_state(self):
        mock_db = MagicMock()
        self._mock_jobs_query(mock_db, [])
        client = self._make_client(mock_db)
        response = client.get("/jobs")
        assert response.status_code == 200

    def test_get_jobs_shows_company_name(self):
        mock_db = MagicMock()
        self._mock_jobs_query(mock_db, [_make_job(company="GoodCorp")])
        client = self._make_client(mock_db)
        response = client.get("/jobs")
        assert "GoodCorp" in response.text


# ---------------------------------------------------------------------------
# Apps router tests
# ---------------------------------------------------------------------------

class TestFilterReasonVisibility:
    """A filtered job must say what filtered it."""

    def _job(self, db, *, reason="title_mismatch", detail="Title 'X' shares no keyword.",
             url="https://ex.com/f1", title="Marketing Manager"):
        import hashlib
        from app.models.job import Job, JobStatus
        job = Job(
            source="linkedin", source_urls=[url], title=title, company="Acme",
            location="NYC", is_remote=False, url=url, description="d",
            experience_level="mid", status=JobStatus.filtered_out,
            filter_reason=reason, filter_detail=detail,
            fetched_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            dedupe_hash=hashlib.sha256(url.encode()).hexdigest()[:32],
        )
        db.add(job)
        db.commit()
        return job

    def test_the_explanation_appears_on_the_job_card(self, db, client):
        self._job(db, detail="Title 'Marketing Manager' shares no keyword with your target roles.")
        response = client.get("/jobs")
        assert response.status_code == 200
        assert "Filtered out" in response.text
        assert "shares no keyword" in response.text

    def test_the_reason_breakdown_is_shown_with_counts(self, db, client):
        self._job(db, url="https://ex.com/a", title="Marketing Manager")
        self._job(db, url="https://ex.com/b", title="Sales Lead")
        self._job(db, reason="no_description",
                  detail="The source returned no description.",
                  url="https://ex.com/c", title="Backend Engineer")
        response = client.get("/jobs")
        assert "Why jobs were filtered out" in response.text
        assert "Title doesn&#39;t match target roles" in response.text \
            or "Title doesn't match target roles" in response.text
        assert "No job description available" in response.text

    def test_can_filter_the_list_to_one_reason(self, db, client):
        self._job(db, url="https://ex.com/a", title="Marketing Manager")
        self._job(db, reason="no_description", detail="No description came back.",
                  url="https://ex.com/b", title="Backend Engineer")
        response = client.get("/jobs?filter_reason=no_description")
        assert "No description came back." in response.text
        assert "shares no keyword" not in response.text

    def test_no_breakdown_when_nothing_is_filtered(self, db, client):
        response = client.get("/jobs")
        assert "Why jobs were filtered out" not in response.text

    def test_reinstating_a_job_clears_its_reason(self, db, client):
        job = self._job(db)
        response = client.post(f"/jobs/{job.id}/override")
        assert response.status_code == 200
        db.refresh(job)
        assert job.filter_reason is None
        assert job.filter_detail is None

    def test_filtering_a_job_by_hand_records_that(self, db, client):
        from app.models.job import JobStatus
        job = self._job(db)
        job.status = JobStatus.matched
        job.filter_reason = None
        job.filter_detail = None
        db.commit()

        client.post(f"/jobs/{job.id}/override")
        db.refresh(job)
        assert job.status == JobStatus.filtered_out
        assert job.filter_reason == "manual"


class TestRunsPage:
    def _record(self, db, **kwargs):
        from app.services.fetch_history import record_run
        counts = {"fetched": 12, "inserted": 4, "merged": 1, "skipped": 7, "stale": 0}
        sources = {
            "linkedin": {"count": 12, "errors": [], "enabled": True},
            "indeed": {"count": 0, "errors": ["Indeed RSS fetch error: 403"],
                       "enabled": True},
        }
        run = record_run(
            db, datetime(2026, 8, 3, 6, 0, tzinfo=timezone.utc), counts, sources,
            per_source_outcome={"linkedin": {"inserted": 4, "merged": 1,
                                             "skipped": 7, "stale": 0}},
            queries=["software engineer"], locations=["New York, NY"],
            **kwargs,
        )
        db.commit()
        return run

    def test_empty_history_explains_itself(self, db, client):
        response = client.get("/runs")
        assert response.status_code == 200
        assert "No runs recorded yet" in response.text

    def test_lists_a_recorded_run(self, db, client):
        self._record(db)
        response = client.get("/runs")
        assert response.status_code == 200
        assert "Aug 03 06:00" in response.text

    def test_shows_each_source_and_its_failure_reason(self, db, client):
        self._record(db)
        response = client.get("/runs")
        assert "linkedin" in response.text
        assert "indeed" in response.text
        assert "Indeed RSS fetch error: 403" in response.text

    def test_shows_the_source_contribution_rollup(self, db, client):
        self._record(db)
        response = client.get("/runs")
        assert "Source contribution" in response.text

    def test_shows_the_queries_the_run_actually_searched(self, db, client):
        self._record(db)
        response = client.get("/runs")
        assert "software engineer" in response.text

    def test_top_boards_leaderboard_lists_producing_boards(self, db, client):
        from app.models.company_board import CompanyBoard
        from app.services.company_boards import record_boards, record_fetch_results
        self._record(db)
        record_boards(db, {"greenhouse": ["topco"]}, origin="discovered")
        record_fetch_results(db, "greenhouse", ["topco"], {"topco": 9})
        db.commit()

        response = client.get("/runs")
        assert "Top company boards" in response.text
        assert "topco" in response.text

    def test_page_survives_missing_history(self, db, client):
        with patch("app.services.fetch_history.recent_runs",
                   side_effect=RuntimeError("no table")):
            response = client.get("/runs")
        assert response.status_code == 200

    def test_limit_is_clamped(self, db, client):
        self._record(db)
        assert client.get("/runs?limit=99999").status_code == 200
        assert client.get("/runs?limit=0").status_code == 200


class TestJobListVisibilityAndSorting:
    """Two reported bugs: unmatched jobs were hidden, and the date sort
    ordered by a value the page never showed."""

    def _job(self, db, *, source="arbeitnow", status=None, posted_at=None,
             fetched_at=None, title="Backend Engineer", url=None):
        import hashlib
        from app.models.job import Job, JobStatus
        url = url or f"https://ex.com/{title}-{source}"
        job = Job(
            source=source, source_urls=[url], title=title, company="Acme",
            location="NYC", is_remote=False, url=url, description="d",
            experience_level="mid", status=status or JobStatus.new,
            fetched_at=fetched_at or datetime(2026, 8, 3, tzinfo=timezone.utc),
            posted_at=posted_at,
            dedupe_hash=hashlib.sha256(url.encode()).hexdigest()[:32],
        )
        db.add(job)
        db.commit()
        return job

    def test_a_job_awaiting_matching_is_visible(self, db, client):
        """Fetched-but-unmatched jobs were invisible; a scoped manual fetch
        skips matching, so they would never have appeared."""
        self._job(db, title="Arbeitnow Role")
        response = client.get("/jobs")
        assert response.status_code == 200
        assert "Arbeitnow Role" in response.text
        assert "Awaiting match" in response.text

    def test_filtering_by_that_source_finds_it(self, db, client):
        self._job(db, source="arbeitnow", title="Arbeitnow Role")
        response = client.get("/jobs?source=arbeitnow")
        assert "Arbeitnow Role" in response.text

    def test_can_filter_to_only_unmatched_jobs(self, db, client):
        from app.models.job import JobStatus
        self._job(db, title="Fresh One")
        self._job(db, title="Scored One", status=JobStatus.matched)
        response = client.get("/jobs?status=new")
        assert "Fresh One" in response.text
        assert "Scored One" not in response.text

    def test_the_source_dropdown_lists_sources_that_have_jobs(self, db, client):
        """The hand-kept list had drifted and omitted several adapters."""
        self._job(db, source="jobicy", title="A")
        self._job(db, source="themuse", title="B")
        response = client.get("/jobs")
        assert 'value="jobicy"' in response.text
        assert 'value="themuse"' in response.text

    def test_newest_first_uses_the_date_the_card_shows(self, db, client):
        """
        A job with no posted_at displays its fetch date. Sorting on posted_at
        alone buried those at the bottom under a recent-looking date.
        """
        self._job(db, title="Old Posted", url="https://ex.com/old",
                  posted_at=datetime(2026, 7, 1, tzinfo=timezone.utc))
        self._job(db, title="No Date But Fresh", url="https://ex.com/fresh",
                  posted_at=None,
                  fetched_at=datetime(2026, 8, 3, tzinfo=timezone.utc))

        body = client.get("/jobs?sort=posted_desc").text
        assert body.index("No Date But Fresh") < body.index("Old Posted")

    def test_oldest_first_reverses_it(self, db, client):
        self._job(db, title="Old Posted", url="https://ex.com/old",
                  posted_at=datetime(2026, 7, 1, tzinfo=timezone.utc))
        self._job(db, title="No Date But Fresh", url="https://ex.com/fresh",
                  posted_at=None,
                  fetched_at=datetime(2026, 8, 3, tzinfo=timezone.utc))

        body = client.get("/jobs?sort=posted_asc").text
        assert body.index("Old Posted") < body.index("No Date But Fresh")

    def test_a_missing_posted_date_is_labelled_honestly(self, db, client):
        self._job(db, title="No Date", posted_at=None)
        response = client.get("/jobs")
        assert "Fetched:" in response.text
        assert "no posted date" in response.text

    def test_a_real_posted_date_is_labelled_posted(self, db, client):
        self._job(db, title="Dated",
                  posted_at=datetime(2026, 8, 1, tzinfo=timezone.utc))
        response = client.get("/jobs")
        assert "Posted: Aug 01, 2026" in response.text


class TestManualFetchTrigger:
    def test_the_runs_page_offers_a_fetch_button_and_source_picker(self, db, client):
        with patch("app.services.fetch_lock.state",
                   return_value={"running": False, "seconds_left": None}):
            response = client.get("/runs")
        assert response.status_code == 200
        assert "Run a fetch now" in response.text
        assert 'name="sources" value="arbeitnow"' in response.text

    def test_triggering_queues_the_task_with_the_chosen_sources(self, db, client):
        with patch("app.services.fetch_lock.state",
                   return_value={"running": False, "seconds_left": None}), \
             patch("app.tasks.fetch.fetch_jobs") as task:
            response = client.post("/runs/trigger",
                                   data={"sources": ["arbeitnow", "dice"]})
        assert response.status_code == 200
        assert task.delay.call_args.kwargs["only"] == ["arbeitnow", "dice"]
        assert "arbeitnow" in response.text

    def test_a_narrow_run_skips_matching(self):
        """Matching costs LLM calls; a single-source smoke test shouldn't."""
        from app.routers.runs import trigger_fetch
        with patch("app.services.fetch_lock.state",
                   return_value={"running": False, "seconds_left": None}), \
             patch("app.tasks.fetch.fetch_jobs") as task:
            trigger_fetch(MagicMock(), sources=["arbeitnow"])
        assert task.delay.call_args.kwargs["match_after"] is False

    def test_a_full_run_still_matches(self):
        from app.routers.runs import trigger_fetch
        with patch("app.services.fetch_lock.state",
                   return_value={"running": False, "seconds_left": None}), \
             patch("app.tasks.fetch.fetch_jobs") as task:
            trigger_fetch(MagicMock(), sources=[])
        assert task.delay.call_args.kwargs["match_after"] is True
        assert task.delay.call_args.kwargs["only"] is None

    def test_unknown_source_names_are_ignored(self):
        from app.routers.runs import trigger_fetch
        with patch("app.services.fetch_lock.state",
                   return_value={"running": False, "seconds_left": None}), \
             patch("app.tasks.fetch.fetch_jobs") as task:
            trigger_fetch(MagicMock(), sources=["arbeitnow", "'; drop table jobs"])
        assert task.delay.call_args.kwargs["only"] == ["arbeitnow"]

    def test_it_refuses_while_a_fetch_is_already_running(self, db, client):
        with patch("app.services.fetch_lock.state",
                   return_value={"running": True, "seconds_left": 120}), \
             patch("app.tasks.fetch.fetch_jobs") as task:
            response = client.post("/runs/trigger", data={})
        task.delay.assert_not_called()
        assert "already running" in response.text

    def test_a_broker_failure_is_reported_not_raised(self, db, client):
        with patch("app.services.fetch_lock.state",
                   return_value={"running": False, "seconds_left": None}), \
             patch("app.tasks.fetch.fetch_jobs") as task:
            task.delay.side_effect = RuntimeError("redis down")
            response = client.post("/runs/trigger", data={})
        assert response.status_code == 200
        assert "Could not queue" in response.text

    def test_the_status_endpoint_reports_a_running_fetch(self, db, client):
        with patch("app.services.fetch_lock.state",
                   return_value={"running": True, "seconds_left": 60}):
            response = client.get("/runs/status")
        assert response.status_code == 200
        assert "Fetch running" in response.text
        assert 'hx-trigger="every 5s"' in response.text

    def test_the_status_endpoint_stops_polling_when_idle(self, db, client):
        with patch("app.services.fetch_lock.state",
                   return_value={"running": False, "seconds_left": None}):
            response = client.get("/runs/status")
        assert "No fetch running" in response.text
        assert "hx-trigger" not in response.text


class TestRetiredBoardVisibility:
    """A board we stopped polling has to be visible, not silently dropped."""

    def _retire(self, db, slug="deadco", ats="greenhouse"):
        from app.models.company_board import CompanyBoard
        from app.services.company_boards import record_boards
        record_boards(db, {ats: [slug]}, origin="discovered")
        board = (
            db.query(CompanyBoard)
            .filter(CompanyBoard.ats == ats, CompanyBoard.slug == slug)
            .one()
        )
        board.active = False
        board.consecutive_empty = 8
        db.commit()
        return board

    def test_retired_board_is_listed_as_not_working(self, db, client):
        self._retire(db)
        response = client.get("/settings")
        assert response.status_code == 200
        assert "Boards not working" in response.text
        assert "deadco" in response.text

    def test_active_boards_are_not_in_the_not_working_list(self, db, client):
        from app.services.company_boards import record_boards
        record_boards(db, {"lever": ["healthyco"]}, origin="discovered")
        db.commit()
        response = client.get("/settings")
        assert "Boards not working" not in response.text

    def test_summary_counts_the_retired_board(self, db, client):
        self._retire(db)
        response = client.get("/settings")
        assert "Not working" in response.text

    def test_retry_puts_the_board_back_into_rotation(self, db, client):
        board = self._retire(db)
        response = client.post(f"/settings/boards/{board.id}/reactivate")
        assert response.status_code == 200
        db.refresh(board)
        assert board.active is True
        assert board.consecutive_empty == 0

    def test_retry_on_an_unknown_board_is_a_404(self, db, client):
        response = client.post(f"/settings/boards/{uuid.uuid4()}/reactivate")
        assert response.status_code == 404

    def test_page_survives_a_registry_failure(self, db, client):
        with patch("app.services.company_boards.retired_boards",
                   side_effect=RuntimeError("db gone")):
            response = client.get("/settings")
        assert response.status_code == 200


class TestAppsRouter:
    def _make_app_obj(self, status="not_applied"):
        from app.models.application import ApplicationStatus
        app_obj = MagicMock()
        app_obj.id = uuid.uuid4()
        app_obj.status = ApplicationStatus(status)
        app_obj.notes = ""
        app_obj.applied_at = None
        app_obj.created_at = None
        app_obj.job = _make_job_model()
        app_obj.documents = []
        return app_obj

    def _make_client(self, mock_db):
        from app.routers.apps import router
        from app.database import get_db
        fastapp = FastAPI()
        fastapp.include_router(router)
        fastapp.dependency_overrides[get_db] = lambda: mock_db
        return TestClient(fastapp)

    def test_get_apps_returns_200(self):
        mock_db = MagicMock()
        mock_db.query.return_value.order_by.return_value.all.return_value = [self._make_app_obj()]
        client = self._make_client(mock_db)
        response = client.get("/apps")
        assert response.status_code == 200

    def test_get_apps_shows_job_title(self):
        mock_db = MagicMock()
        mock_db.query.return_value.order_by.return_value.all.return_value = [self._make_app_obj()]
        client = self._make_client(mock_db)
        response = client.get("/apps")
        assert "Backend Engineer" in response.text

    def test_update_status_returns_200(self):
        app_obj = self._make_app_obj()
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = app_obj
        client = self._make_client(mock_db)
        response = client.post(f"/apps/{app_obj.id}/status", data={"status": "applied"})
        assert response.status_code == 200

    def test_update_status_changes_status(self):
        from app.models.application import ApplicationStatus
        app_obj = self._make_app_obj(status="not_applied")
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = app_obj
        client = self._make_client(mock_db)
        client.post(f"/apps/{app_obj.id}/status", data={"status": "applied"})
        assert app_obj.status == ApplicationStatus.applied

    def test_update_status_returns_404_for_missing(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None
        client = self._make_client(mock_db)
        response = client.post(f"/apps/{uuid.uuid4()}/status", data={"status": "applied"})
        assert response.status_code == 404

    def test_empty_apps_shows_empty_state(self):
        mock_db = MagicMock()
        mock_db.query.return_value.order_by.return_value.all.return_value = []
        client = self._make_client(mock_db)
        response = client.get("/apps")
        assert response.status_code == 200

    def test_update_status_rejects_invalid_status(self):
        app_obj = self._make_app_obj()
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = app_obj
        client = self._make_client(mock_db)
        response = client.post(f"/apps/{app_obj.id}/status", data={"status": "not_a_real_status"})
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Settings router tests
# ---------------------------------------------------------------------------

class TestSettingsRouter:
    def _make_client(self, mock_db):
        from app.routers.settings import router
        from app.database import get_db
        fastapp = FastAPI()
        fastapp.include_router(router)
        fastapp.dependency_overrides[get_db] = lambda: mock_db
        return TestClient(fastapp)

    def _mock_profile(self):
        profile = MagicMock()
        profile.data = {
            "settings": {
                "min_match_score": 70,
                "fetch_interval_hours": 5,
                "min_keyword_skills": 2,
            }
        }
        return profile

    def test_get_settings_returns_200(self):
        mock_db = MagicMock()
        profile = self._mock_profile()
        with patch("app.routers.settings.get_or_create_profile", return_value=profile):
            client = self._make_client(mock_db)
            response = client.get("/settings")
        assert response.status_code == 200

    def test_get_settings_shows_current_values(self):
        mock_db = MagicMock()
        profile = self._mock_profile()
        with patch("app.routers.settings.get_or_create_profile", return_value=profile):
            client = self._make_client(mock_db)
            response = client.get("/settings")
        assert "70" in response.text

    def test_post_settings_saves_values(self):
        mock_db = MagicMock()
        profile = self._mock_profile()
        with patch("app.routers.settings.get_or_create_profile", return_value=profile):
            client = self._make_client(mock_db)
            response = client.post("/settings", data={
                "min_match_score": "80",
                "fetch_interval_hours": "3",
                "min_keyword_skills": "3",
            })
        assert response.status_code == 200
        assert profile.data["settings"]["min_match_score"] == 80

    def test_post_settings_returns_200(self):
        mock_db = MagicMock()
        profile = self._mock_profile()
        with patch("app.routers.settings.get_or_create_profile", return_value=profile):
            client = self._make_client(mock_db)
            response = client.post("/settings", data={
                "min_match_score": "75",
                "fetch_interval_hours": "5",
                "min_keyword_skills": "2",
            })
        assert response.status_code == 200

    def test_get_settings_with_no_profile_settings(self):
        mock_db = MagicMock()
        profile = MagicMock()
        profile.data = {}
        with patch("app.routers.settings.get_or_create_profile", return_value=profile):
            client = self._make_client(mock_db)
            response = client.get("/settings")
        assert response.status_code == 200
        assert "70" in response.text  # default value shown


# ---------------------------------------------------------------------------
# App detail page tests
# ---------------------------------------------------------------------------

class TestAppDetailRouter:
    def _make_app_obj(self):
        from app.models.application import ApplicationStatus, DocType
        app_obj = MagicMock()
        app_obj.id = uuid.uuid4()
        app_obj.status = ApplicationStatus.not_applied
        app_obj.notes = "some notes"
        app_obj.applied_at = None
        app_obj.created_at = None
        app_obj.outreach_contacts = []
        app_obj.job = _make_job_model(
            location="Remote", is_remote=True,
            description="We need a backend engineer.",
        )
        app_obj.documents = []
        return app_obj

    def _make_client(self, mock_db):
        from app.routers.apps import router
        from app.database import get_db
        fastapp = FastAPI()
        fastapp.include_router(router)
        fastapp.dependency_overrides[get_db] = lambda: mock_db
        return TestClient(fastapp)

    def test_get_detail_returns_200(self):
        app_obj = self._make_app_obj()
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = app_obj
        client = self._make_client(mock_db)
        response = client.get(f"/apps/{app_obj.id}")
        assert response.status_code == 200

    def test_get_detail_shows_job_title(self):
        app_obj = self._make_app_obj()
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = app_obj
        client = self._make_client(mock_db)
        response = client.get(f"/apps/{app_obj.id}")
        assert "Backend Engineer" in response.text

    def test_get_detail_returns_404_for_missing(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None
        client = self._make_client(mock_db)
        response = client.get(f"/apps/{uuid.uuid4()}")
        assert response.status_code == 404

    def test_save_notes_returns_200(self):
        app_obj = self._make_app_obj()
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = app_obj
        client = self._make_client(mock_db)
        response = client.post(f"/apps/{app_obj.id}/notes", data={"notes": "Updated notes"})
        assert response.status_code == 200

    def test_save_notes_persists_value(self):
        app_obj = self._make_app_obj()
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = app_obj
        client = self._make_client(mock_db)
        client.post(f"/apps/{app_obj.id}/notes", data={"notes": "my note"})
        assert app_obj.notes == "my note"

    def test_save_notes_returns_404_for_missing(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None
        client = self._make_client(mock_db)
        response = client.post(f"/apps/{uuid.uuid4()}/notes", data={"notes": "x"})
        assert response.status_code == 404

    def test_regenerate_queues_task(self):
        app_obj = self._make_app_obj()
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = app_obj
        client = self._make_client(mock_db)
        with patch("app.routers.apps.generate_docs") as mock_task:
            mock_task.delay = MagicMock()
            response = client.post(f"/apps/{app_obj.id}/regenerate", data={"feedback": "be more concise"})
        assert response.status_code == 200
        mock_task.delay.assert_called_once_with(str(app_obj.id), feedback="be more concise")

    def test_regenerate_returns_404_for_missing(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None
        client = self._make_client(mock_db)
        with patch("app.routers.apps.generate_docs"):
            response = client.post(f"/apps/{uuid.uuid4()}/regenerate", data={"feedback": ""})
        assert response.status_code == 404

    def test_download_doc_returns_404_for_missing(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None
        client = self._make_client(mock_db)
        response = client.get(f"/apps/docs/{uuid.uuid4()}/download")
        assert response.status_code == 404

    def test_download_doc_returns_404_when_file_missing(self):
        from app.models.application import DocType
        doc = MagicMock()
        doc.path = "/storage/resumes/nonexistent.pdf"
        doc.doc_type = DocType.resume
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = doc
        client = self._make_client(mock_db)
        response = client.get(f"/apps/docs/{uuid.uuid4()}/download")
        assert response.status_code == 404


class TestUndatedJobVisibility:
    """
    A job with no posting date skips the fetcher's age check entirely, so some
    of them are long-closed listings passing as fresh. Being able to see them
    as a group is the difference between suspecting that and knowing.
    """

    def _job(self, db, *, title, posted_at=None):
        import hashlib
        from app.models.job import Job, JobStatus
        url = f"https://ex.com/{title}"
        job = Job(
            source="linkedin", source_urls=[url], title=title, company="Acme",
            location="NYC", is_remote=False, url=url, description="d",
            experience_level="mid", status=JobStatus.new,
            fetched_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
            posted_at=posted_at,
            dedupe_hash=hashlib.sha256(url.encode()).hexdigest()[:32],
        )
        db.add(job)
        db.commit()
        return job

    def test_undated_jobs_can_be_isolated(self, db, client):
        self._job(db, title="No Date Role")
        self._job(db, title="Dated Role",
                  posted_at=datetime(2026, 8, 1, tzinfo=timezone.utc))
        body = client.get("/jobs?dated=0").text
        assert "No Date Role" in body
        assert "Dated Role" not in body

    def test_dated_jobs_can_be_isolated(self, db, client):
        self._job(db, title="No Date Role")
        self._job(db, title="Dated Role",
                  posted_at=datetime(2026, 8, 1, tzinfo=timezone.utc))
        body = client.get("/jobs?dated=1").text
        assert "Dated Role" in body
        assert "No Date Role" not in body

    def test_no_filter_shows_both(self, db, client):
        self._job(db, title="No Date Role")
        self._job(db, title="Dated Role",
                  posted_at=datetime(2026, 8, 1, tzinfo=timezone.utc))
        body = client.get("/jobs").text
        assert "No Date Role" in body and "Dated Role" in body

    def test_the_count_tells_you_how_big_the_problem_is(self, db, client):
        for n in range(3):
            self._job(db, title=f"Undated {n}")
        self._job(db, title="Dated Role",
                  posted_at=datetime(2026, 8, 1, tzinfo=timezone.utc))
        assert "No posting date (3)" in client.get("/jobs").text

    def test_the_filter_survives_pagination(self, db, client):
        """Page links rebuild the query string; a filter left out is a filter lost."""
        import hashlib
        from app.models.job import Job, JobStatus
        for n in range(51):
            url = f"https://ex.com/undated-{n}"
            db.add(Job(
                source="linkedin", source_urls=[url], title=f"Undated {n}",
                company="Acme", location="NYC", is_remote=False, url=url,
                description="d", experience_level="mid", status=JobStatus.new,
                fetched_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
                dedupe_hash=hashlib.sha256(url.encode()).hexdigest()[:32],
            ))
        db.commit()
        assert "dated=0" in client.get("/jobs?dated=0").text
