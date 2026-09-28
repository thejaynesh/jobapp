"""
A value saved on the settings page changes what the code does.

CLAUDE.md's failure mode: a control that renders, saves without error, and
changes nothing. `test_settings_coverage` proves no code reads a tunable
straight off `settings`; these store an override on the profile, the way the
page does, and assert the behaviour moves — for every tunable through the
reader the code uses (`live()`), then for the areas one by one.
"""

import time
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.config import live, settings
from app.models.job import Job, JobStatus
from app.models.profile import Profile
from app.services import tunables


def store(db, **overrides):
    db.query(Profile).delete()
    db.add(Profile(data={tunables.STORE_KEY: overrides}))
    db.commit()


def _another(t):
    """A value the page could save for `t` that is not its environment default."""
    default = tunables.default(t)
    if t.kind == "bool":
        return not bool(default)
    if t.kind in ("int", "float"):
        step = 1 if t.kind == "int" else 0.5
        up = float(default) + step
        value = up if t.maximum is None or up <= t.maximum else float(default) - step
        return int(value) if t.kind == "int" else value
    if t.kind == "text":
        return "changed-on-the-page"
    if t.dynamic:
        return "another/model-1"
    return next(c for c in t.choices if c != default)


WITH_ENV = [t for t in tunables.TUNABLES if t.env]


@pytest.mark.parametrize("t", WITH_ENV, ids=[t.key for t in WITH_ENV])
def test_a_saved_value_is_what_the_code_reads(db, t):
    changed = _another(t)
    store(db, **{t.key: changed})
    assert getattr(live(), t.env) == tunables.coerce(t, changed)
    assert getattr(live(), t.env) != tunables.default(t)


# -- how it is read ---------------------------------------------------------


def test_a_request_or_task_reads_the_profile_once(monkeypatch):
    reads = []
    monkeypatch.setattr(tunables, "_load_profile_data",
                        lambda: reads.append(1) or {tunables.STORE_KEY: {"liveness_workers": 3}})
    with tunables.read_once():
        assert [live().LIVENESS_WORKERS for _ in range(50)] == [3] * 50
    assert len(reads) == 1


def test_nothing_is_read_when_nothing_is_asked(monkeypatch):
    reads = []
    monkeypatch.setattr(tunables, "_load_profile_data", lambda: reads.append(1) or {})
    with tunables.read_once():
        pass
    assert reads == []


def test_a_celery_task_gets_its_own_read(monkeypatch):
    from app.celery_app import _read_settings_once_per_task, _release_settings_scope

    reads = []
    monkeypatch.setattr(tunables, "_load_profile_data",
                        lambda: reads.append(1) or {tunables.STORE_KEY: {"liveness_workers": 5}})
    _read_settings_once_per_task(task_id="t1")
    try:
        assert live().LIVENESS_WORKERS == 5 and live().LIVENESS_WORKERS == 5
    finally:
        _release_settings_scope(task_id="t1")
    assert len(reads) == 1
    # Released: the next read goes back to the profile.
    assert live().LIVENESS_WORKERS == 5 and len(reads) == 2


def test_a_board_thread_sees_the_cycle_settings(monkeypatch):
    from app.services.sources import base

    monkeypatch.setattr(tunables, "_load_profile_data",
                        lambda: pytest.fail("a board thread went back to the profile"))
    cfg = tunables.effective_settings({tunables.STORE_KEY: {"max_job_age_days": 9}})
    seen = []
    with base.cycle_settings(cfg):
        base.fetch_boards_concurrently(
            ["a", "b"], lambda slug: seen.append(live().MAX_JOB_AGE_DAYS) or [], "Greenhouse",
            workers=2)
    assert seen == [9, 9]


# -- the schedule -----------------------------------------------------------


class _Redis:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex=None):
        self.values[key] = value


def _dispatch(db, fake, **overrides):
    from app.tasks import schedule

    store(db, **overrides)
    with patch.object(schedule, "_client", return_value=fake), \
            patch.object(schedule.celery_app, "send_task") as send:
        schedule.dispatch_scheduled()
    return [call.args[0] for call in send.call_args_list]


class TestTheScheduleFollowsThePage:
    def test_an_interval_set_on_the_page_decides_when_a_task_runs(self, db):
        fake = _Redis()
        two_hours_ago = time.time() - 2 * 3600
        fake.values["jobapp:schedule:sent:check-posting-liveness"] = str(two_hours_ago)
        sent = _dispatch(db, fake, liveness_interval_hours=1)
        assert "app.tasks.liveness.check_postings" in sent

        fake.values["jobapp:schedule:sent:check-posting-liveness"] = str(two_hours_ago)
        sent = _dispatch(db, fake, liveness_interval_hours=12)
        assert "app.tasks.liveness.check_postings" not in sent

    def test_a_task_never_sent_is_due(self, db):
        from app.tasks.schedule import SCHEDULE

        sent = _dispatch(db, _Redis())
        assert sorted(sent) == sorted(entry.task for entry in SCHEDULE)

    def test_sending_is_remembered(self, db):
        fake = _Redis()
        _dispatch(db, fake)
        assert _dispatch(db, fake) == []

    def test_one_entry_failing_does_not_stop_the_rest(self, db):
        from app.tasks import schedule

        store(db)
        calls = []

        def send(name, kwargs=None):
            calls.append(name)
            if name == "app.tasks.match.match_jobs":
                raise ConnectionError("broker hiccup")

        with patch.object(schedule, "_client", return_value=_Redis()), \
                patch.object(schedule.celery_app, "send_task", side_effect=send):
            sent = schedule.dispatch_scheduled()
        assert "match-new-jobs" not in sent
        assert len(sent) == len(schedule.SCHEDULE) - 1

    def test_the_deep_sweep_keeps_its_argument(self, db):
        from app.tasks import schedule

        store(db)
        with patch.object(schedule, "_client", return_value=_Redis()), \
                patch.object(schedule.celery_app, "send_task") as send:
            schedule.dispatch_scheduled()
        deep = [c for c in send.call_args_list if c.kwargs.get("kwargs") == {"deep": True}]
        assert len(deep) == 1 and deep[0].args[0] == "app.tasks.fetch.sweep_linked_boards"

    def test_beat_ticks_the_dispatcher_rather_than_fixing_intervals(self):
        from app.celery_app import celery_app

        tasks = {e["task"] for e in celery_app.conf.beat_schedule.values()}
        assert "app.tasks.schedule.dispatch_scheduled" in tasks
        for entry in __import__("app.tasks.schedule", fromlist=["SCHEDULE"]).SCHEDULE:
            assert entry.name not in celery_app.conf.beat_schedule


# -- the areas --------------------------------------------------------------


def _job(db, **fields):
    url = f"https://boards.greenhouse.io/acme/jobs/{uuid.uuid4().int % 10**9}"
    job = Job(source="greenhouse", url=url, source_urls=[url], title="Engineer",
              company=fields.pop("company", "Acme"), location="Remote",
              fetched_at=datetime.now(timezone.utc), dedupe_hash=uuid.uuid4().hex,
              status=fields.pop("status", JobStatus.matched), **fields)
    db.add(job)
    db.commit()
    return job


class TestClosedPostings:
    def test_the_page_sets_how_many_are_checked(self, db, monkeypatch):
        from app.services import liveness

        for _ in range(5):
            _job(db)
        monkeypatch.setattr(liveness, "check_url",
                            lambda url, client: liveness.LivenessResult("open", ""))
        store(db, liveness_max_per_cycle=10)
        assert liveness.sweep(db, workers=1)["checked"] == 5
        db.query(Job).update({Job.liveness_checked_at: None})
        db.commit()
        # The ceiling cannot go below 10, so ask the candidates directly for 2.
        assert len(liveness.candidates(db, 2, 3)) == 2

    def test_the_page_can_switch_it_off(self, db):
        from app.tasks.liveness import check_postings

        store(db, liveness_enabled=False)
        assert check_postings()["skipped_reason"] == "disabled"

    def test_the_page_sets_how_long_a_verdict_stands(self, db, monkeypatch):
        from app.services import liveness

        job = _job(db, liveness_checked_at=datetime.now(timezone.utc) - timedelta(days=2))
        store(db, liveness_recheck_days=3)
        assert job not in liveness.candidates(db, 10, live().LIVENESS_RECHECK_DAYS)
        store(db, liveness_recheck_days=1)
        assert job in liveness.candidates(db, 10, live().LIVENESS_RECHECK_DAYS)


class TestMatching:
    def test_the_page_sets_the_languages_you_read(self, db):
        from app.services import matcher

        store(db, match_languages="en, de")
        assert matcher.accepted_languages() == {"en", "de"}

    def test_the_page_can_switch_second_opinions_off(self, db):
        from app.services import matcher

        store(db, deep_match_enabled=False)
        with patch("app.llm.providers.deep_matching_chain",
                   side_effect=AssertionError("asked anyway")):
            assert matcher._deep_score(_job(db), {}, 70.0) is None

    def test_the_page_sets_the_scoring_description_length(self, db):
        from app.services import matcher

        job = _job(db, description="x" * 5000)
        store(db, match_description_chars=2000)
        assert matcher._description_for_prompt(job).startswith("x" * 2000 + "\n")
        store(db, match_description_chars=6000)
        assert matcher._description_for_prompt(job) == "x" * 5000


class TestEverythingElse:
    def test_archiving_follows_the_page(self, db):
        from app.services import archive

        store(db, archive_enabled=False, archive_after_days=90)
        assert archive.enabled() is False
        assert archive._days() == 90

    def test_the_model_log_follows_the_page(self, db):
        from app.services import llm_log

        store(db, llm_log_enabled=False)
        assert llm_log._enabled() is False

    def test_the_display_zone_follows_the_page(self, db):
        from app.services import timefmt

        store(db, display_timezone="Asia/Tokyo")
        assert str(timefmt.zone()) == "Asia/Tokyo"

    def test_the_send_limit_follows_the_page(self, db, monkeypatch):
        from app.services import outreach_sender

        monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com")
        store(db, outreach_send_enabled=True)
        assert outreach_sender.sending_configured() is True
        store(db, outreach_send_enabled=False)
        assert outreach_sender.sending_configured() is False

    def test_a_board_list_on_the_page_is_polled(self, db):
        from app.services.ats_discovery import configured_ats_slugs

        store(db, greenhouse_company_slugs="acme, globex")
        cfg = tunables.effective_settings(db.query(Profile).first().data)
        assert configured_ats_slugs(cfg)["greenhouse"] == ["acme", "globex"]

    def test_a_source_switched_off_on_the_page_is_not_read(self, db):
        from app.services import job_fetcher

        store(db, hiringcafe_enabled=False)
        cfg = tunables.effective_settings(db.query(Profile).first().data)
        with patch("app.services.sources.hiringcafe.fetch",
                   side_effect=AssertionError("read anyway")):
            jobs, stats = job_fetcher._run_all_adapters(
                ["Engineer"], ["Remote"], cfg, {}, {}, only={"hiringcafe"})
        assert jobs == []

    def test_the_provider_models_follow_the_page(self, db, monkeypatch):
        from app.llm import providers

        monkeypatch.setattr(settings, "GEMINI_API_KEY", "k")
        store(db, gemini_model="gemini-9-pro")
        assert providers.configured_providers()["gemini"].model == "gemini-9-pro"

    def test_the_browser_pace_follows_the_page(self, db):
        from app.services import browse_plan

        store(db, browse_max_queued=7)
        assert browse_plan.status(db)["max_per_run"] == 7


class TestTheSettingsPage:
    def test_a_saved_value_shows_in_the_summary(self, client, db):
        client.post("/settings", data={"match_interval_minutes": "45"})
        page = client.get("/settings").text
        assert "45m" in page

    def test_the_switches_panel_no_longer_claims_to_be_env_only(self, client, db):
        page = client.get("/settings").text
        assert "(env-only)" not in page
        assert "set via environment variables" not in page

    def test_a_request_sees_what_the_page_saved(self, client, db):
        from fastapi import APIRouter

        from app.main import app

        client.post("/settings", data={"display_timezone": "Europe/London"})
        router = APIRouter()

        @router.get("/__zone_for_test")
        def zone():
            from app.services import timefmt

            return {"zone": str(timefmt.zone())}

        app.include_router(router)
        try:
            assert client.get("/__zone_for_test").json() == {"zone": "Europe/London"}
        finally:
            app.router.routes = [r for r in app.router.routes
                                 if getattr(r, "path", "") != "/__zone_for_test"]
