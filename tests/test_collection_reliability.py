"""Identity, freshness, coverage and crash recovery at the ingestion boundary."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from app.models.job import Job
from app.models.fetch_run import FetchRun
from app.models.source_listing import FetchBoardRun, ListingRevision, SourceListing
from app.services.collection_ingest import store
from app.services.sources.base import BoardResult, collection_results, cycle_settings


NOW = datetime.now(timezone.utc)


def posting(identifier="1", source="greenhouse", **overrides):
    return {"source": source, "source_job_id": identifier, "ats_slug": "acme",
            "url": f"https://boards.greenhouse.io/acme/jobs/{identifier}",
            "title": "Software Engineer", "company": "Acme", "location": "New York",
            "description": "Build APIs with Python. " * 20, "is_remote": False,
            **overrides}


def test_two_requisitions_with_the_same_title_remain_distinct(db):
    _, first = store(db, posting("1"))
    _, second = store(db, posting("2"))
    assert first.id != second.id
    assert first.dedupe_hash == second.dedupe_hash
    assert db.query(Job).count() == 2


def test_a_legacy_false_alias_does_not_keep_two_requisitions_merged(db):
    _, original = store(db, posting("1"))
    # Historical title-hash merging appended the other requisition's URL.
    original.source_urls = [*original.source_urls, posting("2")["url"]]
    db.flush()
    outcome, second = store(db, posting("2"))
    assert outcome == "inserted"
    assert original.id != second.id


def test_two_tenants_with_local_id_one_remain_distinct(db):
    _, first = store(db, posting("1", source="bamboohr", url="https://alpha.bamboohr.com/careers/1", company="Alpha"))
    _, second = store(db, posting("1", source="bamboohr", url="https://beta.bamboohr.com/careers/1", company="Beta"))
    assert first.id != second.id


def test_cross_posts_with_the_same_employer_requisition_merge(db):
    _, first = store(db, posting("41", source="indeed", url="https://indeed.com/job/abc",
        apply_url="https://boards.greenhouse.io/acme/jobs/41?utm_source=indeed"))
    outcome, second = store(db, posting("41"))
    assert outcome == "merged"
    assert first.id == second.id
    assert db.query(SourceListing).count() == 2


def test_exact_archived_listing_is_skipped_but_new_requisition_survives(db):
    _, job = store(db, posting("1"))
    db.commit()
    db.delete(job)
    db.commit()
    db.expire_all()
    assert store(db, posting("1"))[0] == "skipped"
    assert store(db, posting("2"))[0] == "inserted"


def test_authoritative_pay_change_never_combines_two_different_bands(db):
    _, job = store(db, posting(salary_min=100000, salary_max=120000, salary_currency="USD", salary_period="year"))
    store(db, posting(salary_min=140000, salary_currency="USD", salary_period="year"))
    assert job.salary_min == 140000 and job.salary_max is None


def test_present_authoritative_listing_keeps_aggregator_first_job_open(db):
    from app.services.job_fetcher import _close_vanished
    _, job = store(db, posting("aggregator-id", source="adzuna", url="https://aggregate.example/123",
                             apply_url="https://boards.greenhouse.io/acme/jobs/41"))
    store(db, posting("41"))
    job.board = "greenhouse:acme"
    db.flush()
    assert _close_vanished(db, {("greenhouse", "acme"): {"41"}}) == 0
    assert job.closed_at is None


def test_employer_can_shorten_description_and_change_remote_policy(db):
    _, job = store(db, posting(is_remote=True, updated_at="2026-01-01T00:00:00Z"))
    outcome, same = store(db, posting(description="Build Python APIs onsite.", is_remote=False,
        updated_at="2026-01-02T00:00:00Z"))
    assert outcome == "merged"
    assert same.id == job.id
    assert same.description == "Build Python APIs onsite."
    assert same.is_remote is False
    assert same.description_updated_at is not None
    assert db.query(ListingRevision).count() == 2


def test_old_source_revision_cannot_revert_the_newer_one(db):
    _, job = store(db, posting(description="The current role.", updated_at="2026-02-02T00:00:00Z"))
    store(db, posting(description="A much longer obsolete description. " * 30,
        salary_min=10, updated_at="2026-02-01T00:00:00Z"))
    assert job.description == "The current role."
    assert job.salary_min is None
    assert db.query(ListingRevision).count() == 1


def test_source_revisions_preserve_manual_text_and_salary_band(db):
    _, job = store(db, posting())
    job.description = "My verified description."
    job.salary_min = 120000
    job.salary_max = 160000
    job.manual_fields = ["description", "salary_min"]
    db.flush()
    store(db, posting(description="Employer correction.", salary_min=100, salary_max=200))
    assert job.description == "My verified description."
    assert (job.salary_min, job.salary_max) == (120000, 160000)


def test_identical_observation_updates_freshness_without_another_revision(db):
    _, job = store(db, posting(), now=NOW - timedelta(hours=1))
    assert store(db, posting(), now=NOW)[0] == "skipped"
    assert job.last_seen_at == NOW
    assert db.query(ListingRevision).count() == 1


@pytest.mark.parametrize("source", ["adzuna", "greenhouse"])
def test_earlier_observation_fills_missing_facts_without_reverting_newer_values(db, source):
    _, job = store(db, posting(source=source, salary_min=150000,
        salary_currency="USD", salary_period="year", is_remote=False,
        description="The current onsite role."), now=NOW)
    outcome, same = store(db, posting(source=source, employment_type="contract",
        salary_min=90000, salary_currency="EUR", salary_period="month", is_remote=True,
        description="An older remote description that is much longer. " * 20),
        now=NOW - timedelta(seconds=1))
    db.commit()
    db.refresh(job)

    assert outcome == "merged" and same.id == job.id
    assert job.employment_type == "contract"
    assert (job.salary_min, job.salary_currency, job.salary_period) == (150000, "USD", "year")
    assert job.is_remote is False
    assert job.description == "The current onsite role."
    assert job.last_seen_at == NOW
    assert db.query(ListingRevision).count() == 1


def test_revision_retention_uses_saved_profile_override(db):
    from app.models.profile import Profile
    db.add(Profile(data={"settings": {"collection_revision_history": 2}}))
    db.commit()
    for i in range(4):
        store(db, posting(description=f"Revision number {i} of the requirements."), now=NOW + timedelta(seconds=i))
    assert db.query(ListingRevision).count() == 2


def test_pending_durable_batch_replays_once(db):
    from app.services.collection_batches import replay
    run = FetchRun(started_at=NOW, group="boards", status="running")
    db.add(run)
    db.flush()
    batch = FetchBoardRun(run_id=run.id, source="greenhouse", board="acme",
        status="complete", observed_at=NOW, observed_total=1, returned=1,
        inserted=0, merged=0, dropped=0, payload=[posting()])
    db.add(batch)
    db.commit()
    assert replay(db) == 1
    assert replay(db) == 0
    assert db.query(Job).count() == 1
    assert db.get(FetchBoardRun, batch.id).payload is None


def test_replaying_old_batch_cannot_reopen_a_more_recently_closed_posting(db):
    from app.services.collection_batches import replay
    from app.services.job_fetcher import _close_vanished
    _, job = store(db, posting(), now=NOW - timedelta(days=2))
    db.flush()
    assert _close_vanished(db, {("greenhouse", "acme"): {"another-posting"}}) == 1
    closed_at = job.closed_at
    run = FetchRun(started_at=NOW - timedelta(days=1), group="boards", status="running")
    db.add(run)
    db.flush()
    db.add(FetchBoardRun(run_id=run.id, source="greenhouse", board="acme",
        status="complete", observed_at=NOW - timedelta(days=1), observed_total=1, returned=1,
        inserted=0, merged=0, dropped=0, payload=[posting(employment_type="contract", is_remote=True)]))
    db.commit()
    assert replay(db) == 1
    db.refresh(job)
    assert job.closed_at == closed_at
    assert job.employment_type is None and job.is_remote is False
    assert db.query(SourceListing).one().closed_at is not None


def test_a_completed_board_is_saved_before_the_next_board_fails(db):
    from app.services.collection_batches import sink_for
    from app.services.sources.base import fetch_boards_concurrently
    run = FetchRun(started_at=NOW, group="boards", status="running")
    db.add(run)
    db.commit()
    observed = []

    def fetch_one(slug):
        if slug == "first":
            return [posting(ats_slug="first")]
        observed.append(db.query(Job).count())
        raise RuntimeError("second board failed")

    with collection_results(sink_for(db, run.id)):
        fetch_boards_concurrently(["first", "second"], fetch_one, "greenhouse", workers=1)
    assert observed == [1]
    assert db.query(Job).count() == 1
    assert db.query(FetchBoardRun).filter_by(status="failed").count() == 1


def test_concurrent_sightings_preserve_both_sources_of_detail(db):
    import uuid
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy.orm import sessionmaker
    from app.services import tunables

    identifier = uuid.uuid4().hex
    url = f"https://concurrency.example/{identifier}"
    factory = sessionmaker(bind=db.get_bind().engine, expire_on_commit=False)
    ready = Barrier(2)

    def save(extra):
        with factory() as session:
            ready.wait(timeout=10)
            outcome, job = store(session, posting(identifier, source="adzuna", url=url, **extra))
            session.commit()
            return job.id

    try:
        with patch.object(tunables, "_load_profile_data", return_value={}), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(save, {"salary_min": 150000}),
                       pool.submit(save, {"employment_type": "contract"})]
            ids = [f.result(timeout=30) for f in futures]
        assert ids[0] == ids[1]
        with factory() as session:
            job = session.get(Job, ids[0])
            assert job.salary_min == 150000
            assert job.employment_type == "contract"
    finally:
        # Separate sessions commit for the race; explicitly remove their rows
        # since the outer db fixture cannot roll those transactions back.
        with factory() as session:
            session.query(SourceListing).filter(SourceListing.url == url).delete(synchronize_session=False)
            session.query(Job).filter(Job.url == url).delete(synchronize_session=False)
            session.commit()


def response(payload):
    return httpx.Response(200, json=payload, request=httpx.Request("GET", "https://example.com"))


def test_smartrecruiters_reaches_page_two_and_reports_completion():
    from app.services.sources.smartrecruiters import fetch
    first = [{"id": str(i), "name": "Other role"} for i in range(100)]
    last = [{"id": "wanted", "name": "Software Engineer"}]
    cfg = SimpleNamespace(SMARTRECRUITERS_MAX_PAGES=3, SMARTRECRUITERS_DETAIL_LIMIT=0, ATS_BOARD_FETCH_WORKERS=1)
    with cycle_settings(cfg), collection_results() as results, patch("httpx.get", side_effect=[
        response({"content": first, "totalFound": 101}), response({"content": last, "totalFound": 101})]) as get:
        jobs = fetch(["acme"], queries=["Software Engineer"])
    assert any(j["source_job_id"] == "wanted" for j in jobs)
    assert "offset=100" in get.call_args_list[1].args[0]
    assert results[("smartrecruiters", "acme")].complete is True


def test_smartrecruiters_resumes_capped_pages_instead_of_repeating_first_page():
    from app.services.sources.smartrecruiters import fetch
    rows = [{"id": str(i), "name": "Role"} for i in range(100)]
    cfg = SimpleNamespace(SMARTRECRUITERS_MAX_PAGES=1, SMARTRECRUITERS_DETAIL_LIMIT=0, ATS_BOARD_FETCH_WORKERS=1)
    with cycle_settings(cfg), collection_results() as results, patch("httpx.get", return_value=response({"content": rows, "totalFound": 201})):
        fetch(["acme"])
    result = results[("smartrecruiters", "acme")]
    assert result.complete is False
    assert result.cursor == {"offset": 100}
    with cycle_settings(cfg), collection_results(cursors={("smartrecruiters", "acme"): result.cursor}), patch("httpx.get", return_value=response({"content": [], "totalFound": 201})) as get:
        fetch(["acme"])
    assert "offset=100" in get.call_args.args[0]


def test_per_board_failure_does_not_retire_a_healthy_quiet_board(db):
    from app.models.company_board import CompanyBoard
    from app.services.company_boards import record_fetch_results
    for slug in ("healthy", "limited"):
        db.add(CompanyBoard(ats="greenhouse", slug=slug, origin="discovered", active=True,
            first_seen_at=NOW, last_seen_at=NOW, last_job_count=0, total_job_count=0, consecutive_empty=7))
    db.flush()
    record_fetch_results(db, "greenhouse", ["healthy", "limited"], {}, max_empty_cycles=8, results={
        "healthy": BoardResult(complete=True, total=0),
        "limited": BoardResult(error="429", error_category="rate_limited"),
    })
    healthy = db.query(CompanyBoard).filter_by(slug="healthy").one()
    limited = db.query(CompanyBoard).filter_by(slug="limited").one()
    assert healthy.active and limited.active
    assert healthy.last_success_at is not None
    assert limited.last_success_at is None
    assert limited.consecutive_failures == 1
    assert healthy.next_due_at and limited.next_due_at


def test_retirement_requires_consecutive_confirmed_missing_endpoints(db):
    from app.services.company_boards import record_fetch_results
    from tests.test_company_boards import _board
    board = _board(db, slug="missing-later")
    def record(category):
        record_fetch_results(db, "greenhouse", [board.slug], {}, max_empty_cycles=3,
            results={board.slug: BoardResult(error=category, error_category=category)})
    for _ in range(4):
        record("rate_limit")
    record("not_found")
    assert board.active and board.consecutive_not_found == 1
    record("transport")
    assert board.consecutive_not_found == 0
    record("not_found")
    record("not_found")
    assert board.active
    record("not_found")
    assert not board.active and "404/410" in board.inactive_reason


def test_saved_board_list_is_the_one_validated_and_fetched(db):
    from app.models.profile import Profile
    from app.services.job_fetcher import fetch_and_save_jobs
    db.add(Profile(data={"target_roles": ["Software Engineer"], "settings": {
        "greenhouse_company_slugs": "ui-employer", "ats_slug_validation": True,
        "ats_seed_companies": False, "ats_board_registry": False}}))
    db.commit()
    with patch("app.services.query_expansion.expand_search_queries", return_value=(["Software Engineer"], None)), \
         patch("app.services.ats_validation.validate_configured_slugs", side_effect=lambda slugs, cache: (slugs, {}, {})) as validate, \
         patch("app.services.job_fetcher._run_all_adapters", return_value=([], {})) as adapters:
        fetch_and_save_jobs(db, only={"greenhouse"})
    assert validate.call_args.args[0]["greenhouse"] == ["ui-employer"]
    assert adapters.call_args.args[3]["greenhouse"] == ["ui-employer"]


def test_saved_missing_endpoint_limit_controls_retirement_and_reactivation(db):
    from app.models.profile import Profile
    from app.services.company_boards import record_fetch_results, reactivate
    from tests.test_company_boards import _board
    board = _board(db, slug="confirmed-missing")
    db.add(Profile(data={"settings": {"ats_board_max_empty_cycles": 2}}))
    db.commit()
    def missing():
        record_fetch_results(db, "greenhouse", [board.slug], {},
            results={board.slug: BoardResult(error="HTTP 404", error_category="not_found")})
    missing()
    assert board.active
    missing()
    assert not board.active
    reactivate(db, board.id)
    assert board.next_due_at is None and board.inactive_reason is None
    missing()
    assert board.active and board.consecutive_not_found == 1


def test_failed_adapter_leaves_inspectable_durable_run(db):
    from app.models.profile import Profile
    from app.services.job_fetcher import fetch_and_save_jobs
    db.add(Profile(data={"target_roles": ["Engineer"]}))
    db.commit()
    with patch("app.services.query_expansion.expand_search_queries", return_value=(["Engineer"], None)), \
         patch("app.services.job_fetcher._run_all_adapters", side_effect=RuntimeError("worker failed")):
        fetch_and_save_jobs(db)
    run = db.query(FetchRun).one()
    assert run.status == "failed"
    assert "worker failed" in run.error
    assert run.finished_at is not None


def test_unfinished_run_is_due_for_recovery_without_waiting_full_interval(db):
    from app.tasks import fetch
    db.add(FetchRun(started_at=NOW, group="boards", status="running"))
    db.commit()
    with patch.object(fetch, "SessionLocal", return_value=db), patch.object(db, "close"):
        assert fetch._due("boards") is True
