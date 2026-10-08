"""Unknown sources graduate from captured evidence to usable jobs, without re-visiting."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.config import settings
from app.models.harvest_recipe import HarvestLearningState, HarvestRecipe, HarvestSample
from app.models.job import Job
from app.services import harvest_recipes, harvest_samples, source_learning


HOST = "learning.example"
URL = f"https://{HOST}/api/search"
CUSTOM = {"results": [{"jobTitle": "Platform Engineer", "companyRef": "urn:c:1",
                       "jobUrl": f"https://{HOST}/jobs/101", "jobId": "101"}],
          "companies": [{"id": "urn:c:1", "label": "Example Employer"}]}
RECIPE = {"roots": ["results"], "fields": {"title": ["jobTitle"], "company": ["companyRef"],
           "url": ["jobUrl"], "id": ["jobId"]},
          "join": {"ref": "companyRef", "table": "companies", "key": "id", "take": "label", "into": "company"}}


def capture(db, payload=CUSTOM, url=URL):
    harvest_samples.record(db, HOST, payload, source_url=url)
    db.commit()


def enable(monkeypatch):
    monkeypatch.setattr(settings, "HARVEST_AUTO_LEARN_ENABLED", True)
    monkeypatch.setattr(settings, "HARVEST_LEARN_RETRY_MINUTES", 1)
    monkeypatch.setattr(settings, "HARVEST_LEARN_MAX_ATTEMPTS", 2)


def test_endpoint_identity_ignores_credentials_filters_and_page_number():
    assert harvest_samples.endpoint_key(URL + "?page=2&token=secret&query=python") == "/api/search"
    assert harvest_samples.endpoint_key(URL + "?operationName=SearchJobs&page=2") == "/api/search?operation=SearchJobs"
    assert harvest_samples.endpoint_key(f"https://{HOST}/jobs/54321") == "/jobs/:id"


def test_repeated_payload_keeps_one_example_and_observation_count(db):
    capture(db)
    capture(db)
    rows = harvest_samples.for_host(db, HOST)
    assert len(rows) == 1
    assert rows[0].observations == 2
    assert rows[0].fingerprint and rows[0].shape_hash


def test_duplicate_response_can_graduate_from_unread_to_working_evidence(db):
    capture(db)
    harvest_samples.record(db, HOST, CUSTOM, source_url=URL, found=1, note="Working")
    db.commit()
    rows = harvest_samples.for_host(db, HOST)
    assert len(rows) == 1 and rows[0].found == 1


def test_full_healthy_store_still_admits_smaller_changed_evidence(db, monkeypatch):
    monkeypatch.setattr(settings, "HARVEST_SAMPLES_PER_HOST", 1)
    harvest_samples.record(db, HOST, {**CUSTOM, "padding": "x" * 9000}, source_url=URL, found=1)
    db.commit()
    assert harvest_samples.record(db, HOST, CUSTOM, source_url=URL)
    db.commit()
    assert harvest_samples.for_host(db, HOST)[0].found == 0


def test_new_endpoint_displaces_redundant_large_examples(db, monkeypatch):
    monkeypatch.setattr(settings, "HARVEST_SAMPLES_PER_HOST", 3)
    for n in range(3):
        capture(db, {"padding": "x" * 6000, "n": n}, f"https://{HOST}/config")
    capture(db)
    rows = harvest_samples.for_host(db, HOST, 99)
    assert len(rows) == 3
    assert any(row.endpoint_key == "/api/search" for row in rows)


def test_trim_keeps_company_records_referenced_beyond_first_table_entries(db):
    payload = {**CUSTOM, "companies": [{"id": f"urn:other:{n}", "label": f"Other Employer {n}"}
                                      for n in range(20)] + CUSTOM["companies"]}
    capture(db, payload)
    sample = harvest_samples.for_host(db, HOST)[0]
    assert len(sample.payload["companies"]) <= harvest_samples.MAX_ARRAY_ITEMS * 2
    assert harvest_recipes.validate([sample.payload], RECIPE)["ok"]


def test_learning_replays_jobs_and_keeps_validation_evidence(db):
    capture(db)
    with patch("app.services.harvest_recipes.propose", return_value={"recipe": RECIPE, "error": None}):
        out = harvest_recipes.learn(db, HOST)
    assert out["ok"] and out["replay"]["inserted"] == 1
    assert db.query(Job).filter(Job.url == f"https://{HOST}/jobs/101").one().company == "Example Employer"
    assert harvest_samples.for_host(db, HOST)[0].found == 1
    again = harvest_recipes.learn(db, HOST)
    assert again["ok"] and again["replay"]["inserted"] == 0
    assert db.query(Job).filter(Job.url == f"https://{HOST}/jobs/101").count() == 1


def test_readable_endpoint_does_not_discard_unread_sibling(db):
    capture(db)
    capture(db, {"jobs": [{"title": "Backend Engineer", "companyName": "Other Employer",
                            "url": f"https://{HOST}/jobs/102"}]}, f"https://{HOST}/api/details")
    out = harvest_recipes.learn(db, HOST, endpoint="/api/details")
    assert out["ok"]
    assert harvest_samples.for_host(db, HOST, endpoint="/api/search")[0].found == 0


def test_distinct_endpoints_keep_independent_active_readers(db):
    outcome = {"ok": True, "jobs": 1, "samples": 1, "reason": "validated"}
    harvest_recipes.save(db, HOST, RECIPE, outcome, endpoint="/api/search")
    detail = {"roots": ["detail"], "fields": RECIPE["fields"]}
    harvest_recipes.save(db, HOST, detail, outcome, endpoint="/api/details")
    assert harvest_recipes.active_for(db, HOST, URL) == RECIPE
    assert harvest_recipes.active_for(db, HOST, f"https://{HOST}/api/details?id=101") == detail
    assert db.query(HarvestRecipe).filter_by(host=HOST, status="active").count() == 2


def test_recipe_union_preserves_jobs_the_builtin_already_reads():
    payload = {**CUSTOM, "jobs": [{"title": "Data Engineer", "companyName": "Other Employer",
                                   "url": f"https://{HOST}/jobs/102"}]}
    jobs = harvest_recipes.read_jobs(payload, "learning", URL, RECIPE)
    assert {job["url"] for job in jobs} == {f"https://{HOST}/jobs/101", f"https://{HOST}/jobs/102"}


def test_recipe_union_preserves_full_builtin_description():
    payload = {"jobs": [{"title": "Data Engineer", "companyName": "Example Employer",
                         "url": f"https://{HOST}/jobs/102", "description": "Full description " * 70,
                         "summary": "Short card"}]}
    recipe = {"roots": ["jobs"], "fields": {"title": ["title"], "company": ["companyName"],
               "url": ["url"], "description": ["summary"]}}
    jobs = harvest_recipes.read_jobs(payload, "learning", URL, recipe)
    assert len(jobs) == 1 and len(jobs[0]["description"]) > 600


def test_repair_cannot_regress_existing_sample():
    wrong = {**RECIPE, "roots": ["other"]}
    out = harvest_recipes.validate([CUSTOM], wrong, baseline=RECIPE)
    assert not out["ok"] and "loses postings" in out["reason"]


def test_bad_company_in_minority_is_still_refused():
    payload = {"rows": [{"title": "Engineer", "company": name, "url": f"https://{HOST}/{n}"}
                         for n, name in enumerate(["Good Employer", "Another Employer", "urn:c:7"])]}
    out = harvest_recipes.validate([payload], {"roots": ["rows"],
        "fields": {"title": ["title"], "company": ["company"], "url": ["url"]}})
    assert not out["ok"] and "ids" in out["reason"]


def test_repeated_capture_queues_only_one_task(db, monkeypatch):
    enable(monkeypatch)
    capture(db)
    with patch("app.tasks.source_learning.learn_source.delay") as queue:
        assert source_learning.request_learning(db, HOST, "/api/search")["queued"]
        assert not source_learning.request_learning(db, HOST, "/api/search")["queued"]
    assert queue.call_count == 1


def test_same_evidence_stops_after_attempts_but_new_evidence_reopens(db, monkeypatch):
    enable(monkeypatch)
    capture(db)
    with patch("app.tasks.source_learning.learn_source.delay") as queue:
        source_learning.request_learning(db, HOST, "/api/search")
        state = db.query(HarvestLearningState).one()
        state.attempts = 2
        token = state.claim_token
        db.commit()
        source_learning.finish(db, HOST, "/api/search", token, {"ok": False, "reason": "missing company table"})
        state.next_retry_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
        assert not source_learning.request_learning(db, HOST, "/api/search")["queued"]
        capture(db, {**CUSTOM, "new_evidence": "a distinct response"})
        assert source_learning.request_learning(db, HOST, "/api/search")["queued"]
        assert state.attempts == 0
    assert queue.call_count == 2


def test_trimmed_tail_changes_do_not_reset_learning_attempt_budget(db):
    capture(db, {**CUSTOM, "padding": "x" * 900 + "first"})
    before = source_learning.evidence_hash(harvest_samples.for_host(db, HOST))
    capture(db, {**CUSTOM, "padding": "x" * 900 + "changed"})
    assert source_learning.evidence_hash(harvest_samples.for_host(db, HOST)) == before


def test_lost_worker_claim_is_recoverable(db, monkeypatch):
    enable(monkeypatch)
    capture(db)
    with patch("app.tasks.source_learning.learn_source.delay"):
        source_learning.request_learning(db, HOST, "/api/search")
        state = db.query(HarvestLearningState).one()
        old = state.claim_token
        state.attempted_at = datetime.now(timezone.utc) - timedelta(minutes=20)
        db.commit()
        assert source_learning.request_learning(db, HOST, "/api/search")["queued"]
        assert state.claim_token != old


def test_broker_failure_is_visible_and_retryable(db, monkeypatch):
    enable(monkeypatch)
    capture(db)
    with patch("app.tasks.source_learning.learn_source.delay", side_effect=RuntimeError("broker down")):
        out = source_learning.request_learning(db, HOST, "/api/search")
    state = db.query(HarvestLearningState).one()
    assert not out["queued"] and state.status == "retry"
    assert "broker down" in state.note and state.next_retry_at


def test_analytics_never_queues_a_model(db, monkeypatch):
    enable(monkeypatch)
    capture(db, {"analytics": {"session": "x"}})
    with patch("app.tasks.source_learning.learn_source.delay") as queue:
        assert not source_learning.request_learning(db, HOST, "/api/search")["queued"]
    queue.assert_not_called()


def test_saved_settings_override_enables_real_queue_behavior(db, monkeypatch):
    from app.models.profile import Profile
    from app.services import tunables
    monkeypatch.setattr(settings, "HARVEST_AUTO_LEARN_ENABLED", False)
    capture(db)
    with patch("app.tasks.source_learning.learn_source.delay") as queue:
        assert not source_learning.request_learning(db, HOST, "/api/search")["queued"]
        db.query(Profile).delete()
        db.add(Profile(data={tunables.STORE_KEY: {"harvest_auto_learn_enabled": True}}))
        db.commit()
        assert source_learning.request_learning(db, HOST, "/api/search")["queued"]
    queue.assert_called_once()


def test_unknown_source_does_not_invent_linkedin_posting_urls():
    from app.services.harvest import extract_jobs, source_for_url
    jobs = extract_jobs({"jobs": [{"title": "Backend Engineer", "companyName": "Example Employer", "id": "1234"}]},
                        source=source_for_url(URL))
    assert jobs == []


def test_add_known_board_registers_without_browser(db):
    with patch("app.services.browse_plan.enqueue") as queue:
        out = source_learning.onboard(db, "https://jobs.lever.co/new-source-employer")
    assert out["ok"] and out["status"] == "board_registered"
    queue.assert_not_called()


def test_unknown_source_respects_disabled_browser(db):
    with patch("app.services.browse_plan.enabled", return_value=False), patch("app.services.browse_plan.enqueue") as queue:
        out = source_learning.onboard(db, f"https://{HOST}/careers")
    assert out["status"] == "needs_browser" and "Settings" in out["reason"]
    queue.assert_not_called()


def test_unknown_source_queues_existing_browser_capture(db):
    with patch("app.services.browse_plan.enabled", return_value=True), patch("app.services.browse_plan.is_paused", return_value=False), patch("app.services.browse_plan.enqueue", return_value=1) as queue:
        out = source_learning.onboard(db, f"https://{HOST}/careers")
    assert out["status"] == "waiting_browser"
    assert queue.call_args.kwargs["purpose"] == "source_learning"
    assert "permissions" in out["reason"]


def test_captured_api_response_updates_parent_page_source(db):
    from app.models.harvest_recipe import HarvestLearningState
    source_learning.onboard(db, f"https://{HOST}/careers")
    source_learning.note_capture(db, "https://api.other.example/search", {"found": 2}, page_url=f"https://{HOST}/careers")
    db.commit()
    row = db.query(HarvestLearningState).filter_by(host=HOST, endpoint_key=source_learning.ONBOARDING).one()
    assert row.status == "ready" and row.result["found"] == 2


def test_successful_capture_is_not_overwritten_by_later_analytics(db):
    with patch("app.services.browse_plan.enabled", return_value=False):
        source_learning.onboard(db, f"https://{HOST}/careers")
    source_learning.note_capture(db, URL, {"found": 2}, page_url=f"https://{HOST}/careers")
    source_learning.note_capture(db, URL, {"found": 0}, page_url=f"https://{HOST}/careers")
    db.commit()
    assert db.query(HarvestLearningState).one().status == "ready"


def test_navigation_learning_uses_same_claim_and_retry_controls(db, monkeypatch):
    from app.services import crawl_recipes
    enable(monkeypatch)
    crawl_recipes.record(db, HOST, f"https://{HOST}/search", {
        "controls": [{"tag": "button", "text": "Next", "class": "next"}], "scroll": {"batches": 0}})
    db.commit()
    with patch("app.tasks.source_learning.learn_source.delay") as queue:
        result = source_learning.request_learning(db, HOST, source_learning.NAVIGATION)
    assert result["queued"]
    assert queue.call_args.args[1] == source_learning.NAVIGATION


def test_navigation_telemetry_does_not_reopen_identical_learning_evidence():
    from types import SimpleNamespace
    controls = [{"tag": "button", "text": "Next", "class": "next"}]
    first = SimpleNamespace(source_url=URL, evidence={"controls": controls, "scroll": {"doc_height": 1000}})
    later = SimpleNamespace(source_url=URL, evidence={"controls": controls, "scroll": {"doc_height": 1700}})
    assert source_learning.navigation_hash(first) == source_learning.navigation_hash(later)


def test_layout_drift_retires_previously_successful_reader_and_keeps_current_controls(db):
    from app.services import agent_work, crawl_recipes
    crawl_recipes.save(db, HOST, {"mode": "click", "selector": ".old-next", "max_pages": 10},
                       {"ok": True, "reason": "formerly working"})
    crawl_recipes.note_outcome(db, HOST, 5)
    current = {"controls": [{"tag": "button", "text": "Next", "class": "new-next"}], "scroll": {"batches": 0}}
    for _ in range(3):
        agent_work._learn_to_crawl(db, URL, {"navigation": current}, 1)
    assert crawl_recipes.active_for(db, HOST) is None
    assert crawl_recipes.latest_sample(db, HOST).evidence["controls"][0]["class"] == "new-next"


def test_successful_navigation_resets_drift_failure_streak(db):
    from app.services import crawl_recipes
    crawl_recipes.save(db, HOST, {"mode": "click", "selector": ".next", "max_pages": 10}, {"ok": True})
    for pages in [1, 1, 5, 1, 1]:
        crawl_recipes.note_outcome(db, HOST, pages)
    assert crawl_recipes.active_for(db, HOST) is not None


def test_saved_navigation_failure_limit_controls_relearning(db):
    from app.models.profile import Profile
    from app.services import crawl_recipes
    db.add(Profile(data={"settings": {"crawl_recipe_failure_limit": 2}}))
    db.commit()
    crawl_recipes.save(db, HOST, {"mode": "click", "selector": ".old", "max_pages": 10}, {"ok": True})
    crawl_recipes.note_outcome(db, HOST, 1)
    assert crawl_recipes.active_for(db, HOST)
    crawl_recipes.note_outcome(db, HOST, 1)
    assert crawl_recipes.active_for(db, HOST) is None


def test_pause_cancels_only_this_sources_queued_capture(db):
    from app.services import browser_tasks
    from app.models.browser_task import BrowserTask
    with patch("app.services.browse_plan.enabled", return_value=False):
        source_learning.onboard(db, f"https://{HOST}/careers")
    own = browser_tasks.enqueue(db, "browse_page", {"url": f"https://{HOST}/careers", "purpose": "source_learning"})
    other = browser_tasks.enqueue(db, "browse_page", {"url": "https://other.example/jobs", "purpose": "source_learning"})
    source_learning.pause(db, HOST)
    assert db.get(BrowserTask, own.id).status == "expired"
    assert db.get(BrowserTask, other.id).status == "queued"
    source_learning.note_capture(db, URL, {"found": 1}, page_url=f"https://{HOST}/careers")
    db.commit()
    assert db.query(HarvestLearningState).one().status == "paused"
