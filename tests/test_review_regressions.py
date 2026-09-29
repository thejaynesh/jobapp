"""Availability, concurrency and recovery regressions from the codebase review."""
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest


def test_agent_poll_keeps_the_event_loop_responsive(monkeypatch):
    from app.routers import agent
    from app.services import tunables
    entered, release = threading.Event(), threading.Event()
    threads = []

    def profile():
        threads.append(threading.get_ident())
        entered.set()
        assert release.wait(2)
        return {}

    monkeypatch.setattr(tunables, "_load_profile_data", profile)
    monkeypatch.setattr(agent.browser_tasks, "record_agent_seen", lambda *a: None)

    class Request:
        async def json(self):
            return {"lanes": 0}

    async def exercise():
        loop_thread = threading.get_ident()
        request = asyncio.create_task(agent.lease(Request(), db=None))
        try:
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            assert entered.is_set()
            assert not request.done()
            assert threads == [threads[0]] and threads[0] != loop_thread
        finally:
            release.set()
        assert (await request)["tasks"] == []

    asyncio.run(exercise())


def test_merge_only_fetch_commits_each_batch(db, monkeypatch):
    from tests.test_save_path_batching import cycle
    from tests.test_fetch_task import _std_job, _make_profile_with_targets
    from app.services import job_fetcher
    _make_profile_with_targets(db)
    jobs = [_std_job(title=f"Engineer {i}", url=f"https://review.example/{i}",
                     source_job_id=f"review-{i}") for i in range(5)]
    assert cycle(db, jobs)["inserted"] == 5
    monkeypatch.setattr(job_fetcher, "_COMMIT_EVERY", 2)
    merged, commits = [], []
    original_enrich, original_commit = job_fetcher.enrich_from, db.commit

    def enrich(*args, **kwargs):
        outcome = original_enrich(*args, **kwargs)
        merged.append(1)
        return outcome

    def commit():
        commits.append(len(merged))
        original_commit()

    monkeypatch.setattr(job_fetcher, "enrich_from", enrich)
    monkeypatch.setattr(db, "commit", commit)
    result = cycle(db, [{**job, "description": "Build reliable distributed services. " * 20} for job in jobs])
    assert result["merged"] == 5
    assert 2 in commits and 4 in commits and 5 in commits


@pytest.mark.parametrize("ineligible", ["failed", "has_document"])
def test_generation_limit_only_counts_eligible_applications(db, monkeypatch, ineligible):
    from tests.test_pipeline import make_application
    from app.models.application import ApplicationDocument, DocType
    from app.tasks import generate
    from app.config import settings
    first = make_application(db, suffix="first", generation_status="failed" if ineligible == "failed" else "idle")
    if ineligible == "has_document":
        db.add(ApplicationDocument(application_id=first.id, doc_type=DocType.resume,
                                   path="/not-used.pdf", is_current=True))
    waiting = make_application(db, suffix="waiting", generation_status="idle")
    db.commit()
    wanted = waiting.id
    queued = []
    monkeypatch.setattr(settings, "GENERATION_SWEEP_MAX_PER_RUN", 1)
    monkeypatch.setattr(generate, "SessionLocal", lambda: db)
    monkeypatch.setattr(generate, "queue_generation", lambda app_id: queued.append(app_id) or True)
    assert generate.sweep_generations.run()["never_queued"] == 1
    assert queued == [wanted]


def test_queue_counts_active_queues_and_priority_buckets(monkeypatch):
    from app.services import pipeline
    from app.celery_app import celery_app
    import redis
    keys = []
    client, pipe = MagicMock(), MagicMock()
    client.__enter__.return_value = client
    client.pipeline.return_value.__enter__.return_value = pipe
    pipe.llen.side_effect = lambda key: keys.append(key)
    pipe.execute.side_effect = lambda: [2 if k == "batch" else 3 if k == "interactive\x06\x163" else 0 for k in keys] + [1]
    monkeypatch.setattr(redis.Redis, "from_url", lambda *a, **k: client)
    result = pipeline.queue_depth()
    assert result["waiting"] == 5 and result["claimed"] == 1
    assert result["queues"]["batch"] == 2 and result["queues"]["interactive"] == 3
    assert "celery" not in keys


@pytest.mark.parametrize("same_message, cap", [(True, 30), (False, 1)])
def test_concurrent_sends_reserve_delivery_and_daily_capacity(monkeypatch, same_message, cap):
    from tests.conftest import TestSessionLocal
    from app.models.outreach import Contact, OutreachMessage
    from app.services import outreach_sender
    monkeypatch.setattr(outreach_sender, "sending_blocked_reason", lambda: "")
    monkeypatch.setattr(outreach_sender.settings, "SMTP_FROM_EMAIL", "sender@example.test")
    monkeypatch.setattr(outreach_sender.settings, "OUTREACH_MAX_SENDS_PER_DAY", cap)
    with TestSessionLocal() as db:
        contact = Contact(company="Review", company_key="review", email="recipient@example.test", email_status="verified")
        db.add(contact)
        db.flush()
        messages = [OutreachMessage(contact_id=contact.id, channel="email", body="Test", status="draft")
                    for _ in range(1 if same_message else 2)]
        db.add_all(messages)
        db.commit()
        ids, contact_id = [m.id for m in messages], contact.id
    rendezvous, refused = threading.Barrier(2), threading.Event()
    delivered = []

    def deliver(mail):
        delivered.append(mail["Message-ID"])
        assert refused.wait(5), "second request did not fail while delivery was reserved"

    monkeypatch.setattr(outreach_sender, "_deliver", deliver)

    def send(message_id):
        with TestSessionLocal() as db:
            message = db.get(OutreachMessage, message_id)
            rendezvous.wait(timeout=5)
            try:
                outreach_sender.send_message(db, message)
                return "sent"
            except outreach_sender.SendError as exc:
                refused.set()
                return str(exc)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(send, ids[0]), pool.submit(send, ids[-1])]
            results = [f.result(timeout=10) for f in futures]
        assert results.count("sent") == 1
        assert len(delivered) == 1
        assert any(("being sent" if same_message else "Daily send limit") in r for r in results)
    finally:
        with TestSessionLocal() as db:
            db.query(OutreachMessage).filter(OutreachMessage.id.in_(ids)).delete(synchronize_session=False)
            db.query(Contact).filter_by(id=contact_id).delete()
            db.commit()


def test_ingestion_rolls_back_partial_commits_and_recovers(db, monkeypatch):
    from app.models.browser_task import BrowserTask
    from app.models.profile import Profile
    from app.services import browser_tasks, agent_work
    from app.tasks import browse
    from app.config import settings
    profile = Profile(data={"original": True})
    db.add(profile)
    task = browser_tasks.enqueue(db, "fetch_json")
    browser_tasks.lease(db, agent_id="review")

    def broken(work, current):
        work.query(Profile).first().data = {"partial": True}
        current.result = {"lost": True}
        work.commit()
        raise RuntimeError("temporary handler failure")

    monkeypatch.setitem(agent_work.RESULT_HANDLERS, "fetch_json", broken)
    done = browser_tasks.complete(db, task.id, {"json": {"saved": True}}, agent_id="review")
    assert done.status == "done" and done.ingestion_status == "retry"
    assert "temporary handler failure" in done.ingestion_error
    assert done.result == {"json": {"saved": True}}
    db.refresh(profile)
    assert profile.data == {"original": True}

    done.ingestion_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    task_id = done.id
    applied = []

    def recover(work, current):
        applied.append(current.id)
        work.query(Profile).first().data = {"recovered": True}
        work.commit()

    monkeypatch.setitem(agent_work.RESULT_HANDLERS, "fetch_json", recover)
    monkeypatch.setattr(browse, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    assert browse.retry_ingestion.run() == {"checked": 1, "recovered": 1}
    assert browse.retry_ingestion.run() == {"checked": 0, "recovered": 0}
    db.refresh(done)
    assert done.ingestion_status == "done" and done.ingestion_error is None
    assert applied == [task_id]


def test_unprocessed_results_are_not_pruned(db):
    from app.models.browser_task import BrowserTask
    from app.services import browser_tasks
    now = datetime.now(timezone.utc)
    task = BrowserTask(kind="fetch_json", status="done", result={"saved": True},
                       ingestion_status="retry", completed_at=now - timedelta(days=30), expires_at=now)
    db.add(task)
    db.commit()
    task_id = task.id
    browser_tasks.prune(db, days=14)
    assert db.get(BrowserTask, task_id) is not None


def test_concurrent_ingestion_applies_saved_result_once(monkeypatch):
    from tests.conftest import TestSessionLocal
    from app.models.browser_task import BrowserTask
    from app.services import agent_work
    with TestSessionLocal() as db:
        task = BrowserTask(kind="ping", status="done", ingestion_status="pending",
                           result={"saved": True}, expires_at=datetime.now(timezone.utc))
        db.add(task)
        db.commit()
        task_id = task.id
    entered, release = threading.Event(), threading.Event()
    effects = []

    def handler(work, current):
        effects.append(current.id)
        entered.set()
        assert release.wait(5)
        current.result = {"applied": True}
        work.commit()

    monkeypatch.setitem(agent_work.RESULT_HANDLERS, "ping", handler)

    def ingest():
        with TestSessionLocal() as db:
            return agent_work.ingest(db, db.get(BrowserTask, task_id))

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(ingest)
            try:
                assert entered.wait(5)
                assert pool.submit(ingest).result(timeout=3) is False
            finally:
                release.set()
            assert first.result(timeout=5) is True
        assert effects == [task_id]
    finally:
        with TestSessionLocal() as db:
            db.query(BrowserTask).filter_by(id=task_id).delete()
            db.commit()


def test_failed_chunk_commit_stops_the_fetch(db, monkeypatch):
    from tests.test_save_path_batching import cycle
    from tests.test_fetch_task import _std_job, _make_profile_with_targets
    from app.services import job_fetcher
    _make_profile_with_targets(db)
    jobs = [_std_job(title=f"Chunk {i}", url=f"https://chunk.example/{i}", source_job_id=str(i)) for i in range(5)]
    assert cycle(db, jobs)["inserted"] == 5
    merged = []
    original_enrich, original_commit = job_fetcher.enrich_from, db.commit
    def enrich(*args, **kwargs):
        merged.append(1)
        return original_enrich(*args, **kwargs)
    def commit():
        if len(merged) == 2:
            raise RuntimeError("connection lost committing chunk")
        original_commit()
    monkeypatch.setattr(job_fetcher, "_COMMIT_EVERY", 2)
    monkeypatch.setattr(job_fetcher, "enrich_from", enrich)
    monkeypatch.setattr(db, "commit", commit)
    with pytest.raises(RuntimeError, match="committing chunk"):
        cycle(db, [{**job, "description": "Improved description. " * 30} for job in jobs])
    assert len(merged) == 2


def test_readiness_reports_database_failure(monkeypatch):
    from app.main import readiness
    db = MagicMock()
    assert readiness(db).status_code == 200
    db.execute.side_effect = RuntimeError("database unavailable")
    assert readiness(db).status_code == 503


def test_ingestion_retry_interval_uses_saved_setting(db, monkeypatch):
    from app.config import settings
    from app.models.profile import Profile
    from app.services import agent_work, browser_tasks
    from app.services.tunables import STORE_KEY
    db.add(Profile(data={STORE_KEY: {"agent_ingest_retry_minutes": 17}}))
    db.commit()
    monkeypatch.setattr(settings, "AGENT_INGEST_RETRY_MINUTES", 5)
    def fail(*args):
        raise RuntimeError("try again")
    monkeypatch.setitem(agent_work.RESULT_HANDLERS, "ping", fail)
    task = browser_tasks.enqueue(db, "ping")
    browser_tasks.lease(db)
    started = datetime.now(timezone.utc)
    done = browser_tasks.complete(db, task.id, {"saved": True})
    assert done.ingestion_status == "retry"
    assert timedelta(minutes=16) < done.ingestion_retry_at - started < timedelta(minutes=18)
