from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.models.application import ApplicationStatus, ApplicationDocument, DocType
from app.models.intelligence import ApplicationEvent, DecisionEvent
from app.models.profile import Profile
from app.services import application_history as history, application_mail, capacity, daily_plan
from app.services import document_edit, document_evidence, evidence, outcome_learning, quality_benchmark, semantic, tracker
from tests.test_application_history import application
from tests.test_document_edit import setup as document_setup


def test_today_page_is_usable_without_data(client, db):
    response = client.get("/today")
    assert response.status_code == 200
    assert "Your next steps" in response.text and "Silence is not treated as rejection" in response.text


def test_today_budget_due_actions_and_exposure_idempotence(client, db):
    app = application(db)
    tracker.set_status(db, app, ApplicationStatus.applied)
    app.next_action, app.next_action_due = "Assessment deadline", datetime.now(timezone.utc).date()
    db.commit()
    plan = daily_plan.build(db, {}, minutes=5)
    assert plan["planned_minutes"] == 5
    assert plan["actions"][0]["title"] == "Assessment deadline"
    assert client.get("/today?minutes=5").status_code == 200
    assert client.get("/today?minutes=5").status_code == 200
    assert db.query(DecisionEvent).filter(DecisionEvent.kind == "shown").count() == 1


def test_plan_does_not_repeat_employers_or_closed_jobs(db):
    first, second, closed = [application(db) for _ in range(3)]
    closed.job.closed_at = datetime.now(timezone.utc)
    db.flush()
    plan = daily_plan.build(db, {}, minutes=60)
    assert len([a for a in plan["actions"] if a["kind"] == "apply"]) == 1
    assert all(a["job"] is not closed.job for a in plan["actions"])


def test_clarification_saved_and_forgotten(client, db):
    key = "r-" + "a" * 20
    assert client.post("/today/answer", data={"question_id": key, "answer": "I have used Python for three years."}).status_code == 200
    assert key in db.query(Profile).first().data["requirement_answers"]
    assert client.post(f"/today/answers/{key}/delete").status_code == 200
    assert key not in db.query(Profile).first().data["requirement_answers"]


def test_plan_dismissal_is_a_preference_not_an_employer_rejection(client, db):
    app = application(db)
    app.job.favourite = True
    db.commit()
    response = client.post(f"/today/dismiss/{app.job_id}", data={"reason": "role"})
    assert response.status_code == 200
    db.refresh(app)
    assert app.status == ApplicationStatus.not_applied
    assert app.job.filter_reason == "manual"
    assert not app.job.favourite
    assert db.query(DecisionEvent).filter(DecisionEvent.kind == "no").count() == 1
    assert db.query(ApplicationEvent).count() == 0


def test_history_ui_and_submission_confirmation(client, db):
    app, docs = document_setup(db)
    tracker.set_status(db, app, ApplicationStatus.applied)
    db.commit()
    page = client.get(f"/apps/{app.id}")
    assert page.status_code == 200 and "Current linkage: inferred" in page.text
    page = client.post(f"/apps/{app.id}/submitted-document", data={"document_id": str(docs["resume"].id)})
    assert page.status_code == 200 and "Current linkage: user" in page.text


def test_correction_rebuilds_status_and_can_be_undone(client, db):
    app = application(db)
    tracker.set_status(db, app, ApplicationStatus.applied)
    tracker.set_status(db, app, ApplicationStatus.interviewing)
    db.commit()
    event = db.query(ApplicationEvent).filter(ApplicationEvent.kind == "interview_invited").one()
    response = client.post(f"/apps/{app.id}/events/{event.id}/correct", data={"note": "Wrong application"})
    assert response.status_code == 200
    db.refresh(app)
    assert app.status == ApplicationStatus.applied
    correction = db.query(ApplicationEvent).filter(ApplicationEvent.kind == "correction").one()
    client.post(f"/apps/{app.id}/events/{event.id}/correct", data={"note": "Repeated click"})
    assert db.query(ApplicationEvent).filter(ApplicationEvent.kind == "correction").count() == 1
    client.post(f"/apps/{app.id}/events/{correction.id}/correct", data={"note": "Restore the invitation"})
    db.refresh(app)
    assert app.status == ApplicationStatus.interviewing
    client.post(f"/apps/{app.id}/events/{event.id}/correct", data={"note": "Retract after undo"})
    db.refresh(app)
    assert app.status == ApplicationStatus.applied


def test_mail_requires_role_identity_and_deduplicates(db):
    app, other = application(db), application(db)
    other.job.title = "Designer"
    for row in (app, other):
        tracker.set_status(db, row, ApplicationStatus.applied)
    message = EmailMessage()
    message["Subject"] = "Example: Engineer application received"
    message["Message-ID"] = "<receipt-123@example.org>"
    message.set_content("Thank you for applying for Engineer at Example.")
    assert application_mail.propose(db, message) == 1
    assert application_mail.propose(db, message) == 0
    suggestion = history.pending(db)[0]
    assert suggestion.application_id == app.id
    history.review_mail(db, suggestion, True, {})
    assert history.pending(db) == []


def test_mail_company_alone_and_out_of_office_are_not_application_updates(db):
    app = application(db)
    tracker.set_status(db, app, ApplicationStatus.applied)
    for subject, body in [("Example", "Thank you for applying to Example."),
                          ("Out of office", "Engineer at Example: thank you for applying")]:
        message = EmailMessage()
        message["Subject"] = subject
        message.set_content(body)
        assert application_mail.propose(db, message) == 0


def test_mature_silence_is_unknown_and_interview_then_rejection_positive(db):
    now = datetime.now(timezone.utc)
    silent, interviewed, recent = [application(db) for _ in range(3)]
    for app in (silent, interviewed):
        tracker.set_status(db, app, ApplicationStatus.applied, now=now - timedelta(days=30))
    tracker.set_status(db, recent, ApplicationStatus.applied, now=now - timedelta(days=1))
    tracker.set_status(db, interviewed, ApplicationStatus.interviewing, now=now - timedelta(days=10))
    tracker.set_status(db, interviewed, ApplicationStatus.rejected, now=now - timedelta(days=5))
    db.flush()
    report = outcome_learning.report(db, {}, now)
    assert (report["mature"], report["unknown"], report["immature"], report["interviews"]) == (2, 1, 1, 1)
    assert outcome_learning.fit(db, {}, now)["usable"] is False


def test_application_outcome_uses_first_decision_snapshot(db):
    app = application(db)
    app.job.llm_score = 80
    tracker.set_status(db, app, ApplicationStatus.applied)
    app.job.llm_score = 20
    tracker.set_status(db, app, ApplicationStatus.interviewing)
    records = db.query(DecisionEvent).filter(DecisionEvent.kind == "yes").all()
    assert len(records) == 1 and records[0].payload["score"] == 80


@pytest.mark.parametrize("sample", [
    {"cpu_steal": 90}, {"cpu_pressure": 90}, {"memory_pressure": 90},
    {"interactive_queue_seconds": 200}, {"latency_p95_ms": 2100, "page_samples": 5},
])
def test_capacity_defers_for_each_relevant_signal(sample):
    assert capacity.reasons(sample, {})


def test_capacity_does_not_treat_missing_telemetry_as_zero_or_pause():
    assert capacity.status({})["reason"] == "Capacity telemetry unavailable"
    assert capacity.allow_background({})
    assert not capacity.reasons({"latency_p95_ms": 20000, "page_samples": 1}, {})


def test_capacity_cooldown_survives_a_healthy_sample(monkeypatch):
    class Redis:
        def __init__(self): self.data = {}
        def get(self, key): return self.data.get(key)
        def set(self, key, value, **_): self.data[key] = value
        def ttl(self, key): return 200 if key in self.data else -2
    redis = Redis()
    monkeypatch.setattr(capacity, "_client", lambda: redis)
    monkeypatch.setattr(capacity, "metrics", lambda *_: {"cpu_steal": 95})
    assert capacity.status({})["paused"]
    redis.data.pop(capacity.PREFIX + "status")
    monkeypatch.setattr(capacity, "metrics", lambda *_: {"cpu_steal": 0})
    assert capacity.status({})["paused"]


def test_stale_resume_edit_never_compiles(db, monkeypatch):
    app, docs = document_setup(db)
    old = docs["resume"]
    old.is_current = False
    db.add(ApplicationDocument(application_id=app.id, doc_type=DocType.resume, version=2,
        path="/tmp/new.pdf", is_current=True, content=old.content))
    db.commit()
    compile = Mock()
    monkeypatch.setattr("app.services.doc_generator.compile_resume_one_page", compile)
    with pytest.raises(document_edit.StaleEdit):
        document_edit.save_resume(db, app, old, {"summary": "A stale edit"})
    compile.assert_not_called()


def test_edit_rechecks_its_base_after_compilation(db, monkeypatch):
    app, docs = document_setup(db)
    old = docs["resume"]
    def compile_while_another_edit_finishes(context, path):
        old.is_current = False
        db.add(ApplicationDocument(application_id=app.id, doc_type=DocType.resume, version=2,
            path="/tmp/concurrent.pdf", is_current=True, content=old.content))
        db.commit()
        return path
    monkeypatch.setattr("app.services.doc_generator.compile_resume_one_page", compile_while_another_edit_finishes)
    with pytest.raises(document_edit.StaleEdit):
        document_edit.save_resume(db, app, old, {"summary": "Must not replace the concurrent edit"})
    assert db.query(ApplicationDocument).filter(ApplicationDocument.doc_type == DocType.resume, ApplicationDocument.is_current.is_(True)).one().path == "/tmp/concurrent.pdf"


def test_generation_rejects_a_base_changed_during_model_work():
    from unittest.mock import patch
    from tests.test_doc_generator import TestGenerateDocuments, _mock_db_for_generate, _make_app
    from app.services.doc_generator import generate_documents, DocGenerationError
    db = _mock_db_for_generate()
    stack, _ = TestGenerateDocuments()._patches()
    with stack, patch("app.services.doc_generator._next_version", side_effect=[2, 2, 3]):
        with pytest.raises(DocGenerationError, match="changed during generation"):
            generate_documents(db, _make_app())
    db.add.assert_not_called()
    db.rollback.assert_called()


def test_edit_preview_does_not_save_or_compile(client, db, monkeypatch):
    app, docs = document_setup(db)
    compile = Mock()
    monkeypatch.setattr("app.services.doc_generator.compile_resume_one_page", compile)
    before = db.query(ApplicationDocument).count()
    response = client.post(f"/apps/{app.id}/docs/{docs['resume'].id}/preview-edit", data={"summary": "A new summary <script>"})
    assert response.status_code == 200 and "+A new summary &lt;script&gt;" in response.text
    assert db.query(ApplicationDocument).count() == before
    compile.assert_not_called()


def test_curation_never_moves_metrics_to_another_employer():
    entries = [{"id": "a", "company": "A", "bullets": ["Cut Python latency by 40%", "Kept records"]},
               {"id": "b", "company": "B", "bullets": ["Built Java services"]}]
    chosen = document_evidence.curate(entries, SimpleNamespace(description="Python latency"), {"settings": {"document_bullets_per_entry": 1}})
    assert chosen[0]["bullets"] == ["Cut Python latency by 40%"]
    assert chosen[1]["bullets"] == ["Built Java services"]


def test_semantic_off_never_calls_provider(db, monkeypatch):
    embed = Mock(side_effect=AssertionError("must not call"))
    monkeypatch.setattr(semantic, "_embed", embed)
    assert semantic.update(db, {})["status"] == "off"
    assert semantic.cached_scores(db, [], {}) == {}
    embed.assert_not_called()


def test_semantic_cache_key_changes_with_model_and_content():
    assert semantic.key("one", "model") != semantic.key("two", "model")
    assert semantic.key("one", "model") != semantic.key("one", "another-model")
    assert semantic.cosine([1, 0], [0, 1]) == 0
    assert semantic.cosine([1, 0], [1, 0]) == 1
    assert semantic.cosine([1], [1, 0]) == 0


@pytest.mark.parametrize("data", [
    [{"index": 0, "embedding": [1, 2]}, {"index": 0, "embedding": [3, 4]}],
    [{"index": 0, "embedding": [True]}, {"index": 1, "embedding": [1]}],
    [{"index": 0, "embedding": [1]}, {"index": 1, "embedding": [1, 2]}],
    ["invalid", {"index": 1, "embedding": [1]}],
])
def test_semantic_rejects_malformed_provider_vectors(monkeypatch, data):
    import httpx
    from app.config import settings
    client = httpx.Client
    monkeypatch.setattr(settings, "SEMANTIC_BASE_URL", "https://embedding.invalid/v1")
    monkeypatch.setattr(semantic.httpx, "Client", lambda **kw: client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": data})), **kw))
    with pytest.raises(ValueError):
        semantic._embed(["first", "second"], "test-model")


def test_semantic_orders_vectors_by_provider_index(monkeypatch):
    import httpx
    from app.config import settings
    client = httpx.Client
    monkeypatch.setattr(settings, "SEMANTIC_BASE_URL", "https://embedding.invalid/v1")
    data = [{"index": 1, "embedding": [2, 3]}, {"index": 0, "embedding": [4, 5]}]
    monkeypatch.setattr(semantic.httpx, "Client", lambda **kw: client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": data})), **kw))
    assert semantic._embed(["first", "second"], "test-model") == [[4, 5], [2, 3]]


def test_source_backoff_preserves_periodic_probes(db):
    from app.models.fetch_run import FetchRun, FetchSourceRun
    now = datetime.now(timezone.utc)
    for hours in (1, 2, 3):
        run = FetchRun(started_at=now - timedelta(hours=hours, minutes=5), finished_at=now - timedelta(hours=hours), status="ok")
        db.add(run)
        db.flush()
        db.add(FetchSourceRun(run_id=run.id, source="sample", enabled=True, status="ok", inserted=0, merged=0))
    db.flush()
    cfg = SimpleNamespace(ADAPTIVE_SOURCE_ENABLED=True, ADAPTIVE_SOURCE_MAX_HOURS=24)
    assert "sample" in capacity.source_waits(db, cfg, now)
    assert "sample" not in capacity.source_waits(db, cfg, now + timedelta(hours=25))


def test_application_channel_is_explicit_and_separates_cohorts(client, db):
    app = application(db)
    tracker.set_status(db, app, ApplicationStatus.applied, now=datetime.now(timezone.utc) - timedelta(days=30))
    db.commit()
    assert client.post(f"/apps/{app.id}/channel", data={"channel": "referral"}).status_code == 200
    report = outcome_learning.report(db, {})
    assert report["channels"]["referral"]["mature"] == 1
    assert report["channels"]["referral"]["unknown"] == 1


def test_quality_benchmark_requires_paid_opt_in(monkeypatch):
    run = Mock()
    monkeypatch.setattr("app.services.match_eval.run", run)
    with pytest.raises(ValueError, match="allow-paid"):
        quality_benchmark.compare([], {})
    run.assert_not_called()


def test_quality_benchmark_compares_one_change_on_same_fixture(monkeypatch):
    from app.services.match_eval import LabelledJob
    run = Mock(return_value={"agreement": 100})
    monkeypatch.setattr("app.services.match_eval.run", run)
    profile = {"settings": {"match_evidence_mode": "assist"}}
    result = quality_benchmark.compare([LabelledJob("good", fields={"title": "Engineer"})], profile, allow_paid=True)
    assert [call.args[1]["settings"]["match_evidence_mode"] for call in run.call_args_list] == ["shadow", "assist"]
    assert result["requested_assessments"] == 2 and profile["settings"]["match_evidence_mode"] == "assist"


def test_observations_export_and_deletion(client, db):
    app = application(db)
    history.record_decision(db, app.job, {}, "yes")
    history.append(db, app, "receipt")
    db.commit()
    assert len(client.get("/intelligence/export").json()["decisions"]) == 1
    assert client.post("/intelligence/decisions/delete", data={"confirm": "wrong"}).status_code == 422
    assert client.post("/intelligence/decisions/delete", data={"confirm": "delete ranking observations"}).status_code == 200
    assert db.query(DecisionEvent).count() == 0
    assert db.query(ApplicationEvent).count() == 1
