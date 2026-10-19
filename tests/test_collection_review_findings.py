"""Regressions from the review of source learning and durable collection."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.models.job import Job
from tests.test_fetch_task import _make_profile_with_targets, _std_job
from tests.test_save_path_batching import cycle

NOW = datetime.now(timezone.utc)


def _cycle_through_record(db, source, jobs):
    """A cycle whose adapter reports through `_record`, as the real ones do."""
    from app.services import job_fetcher

    def adapters(*args, **kwargs):
        stats = {}
        job_fetcher._record(stats, source, jobs)
        stats[source]["enabled"] = True
        return list(jobs), stats

    with patch("app.services.query_expansion.expand_search_queries",
               return_value=(["Software Engineer"], None)), \
         patch("app.services.job_fetcher._run_all_adapters", side_effect=adapters):
        return job_fetcher.fetch_and_save_jobs(db)


def test_aggregator_redirect_is_followed_and_meets_the_employers_copy(db, monkeypatch):
    from app.services import link_resolver
    _make_profile_with_targets(db)
    board_url = "https://boards.greenhouse.io/acme/jobs/4242"
    cycle(db, [_std_job(source="greenhouse", source_job_id="4242", url=board_url,
                        title="Platform Engineer")])
    redirect = "https://www.adzuna.com/land/ad/5123456789"
    followed = []

    def resolve_urls(urls, **kwargs):
        followed.extend(urls)
        return [link_resolver.ResolvedLink(original_url=u, final_url=board_url) for u in urls]

    monkeypatch.setattr(link_resolver, "resolve_urls", resolve_urls)
    counts = _cycle_through_record(db, "adzuna", [
        _std_job(source="adzuna", source_job_id="az-1", url=redirect, title="Platform Engineer")])

    assert followed == [redirect]
    assert db.query(Job).count() == 1
    assert counts["merged"] == 1
    assert "adzuna" in db.query(Job).one().seen_by


def test_a_recurring_bad_row_does_not_rest_a_source_that_is_working(db):
    from app.models.fetch_run import FetchSourceRun
    from app.services.fetch_history import failing_streaks, record_run, resting_sources
    stats = {"greenhouse": {"count": 2001, "errors": [], "enabled": True}}
    for minutes in range(12):
        record_run(db, NOW - timedelta(minutes=60 - minutes), {"fetched": 2001, "skipped": 2000, "dropped": 1},
                   stats, {"greenhouse": {"skipped": 2000, "dropped": 1}})
    assert {row.status for row in db.query(FetchSourceRun)} == {"partial"}
    assert "greenhouse" not in failing_streaks(db)
    assert "greenhouse" not in resting_sources(db, threshold=10)

    # Everything it returned was dropped: that is still a failure.
    record_run(db, NOW, {"fetched": 3, "dropped": 3}, stats, {"greenhouse": {"dropped": 3}})
    assert failing_streaks(db)["greenhouse"] == 1


def test_a_board_row_without_an_id_is_not_closed_for_being_unlisted(db):
    from app.services.collection_ingest import store
    from app.services.job_fetcher import _close_vanished

    def pinpoint(identifier, path):
        return {"source": "pinpoint", "source_job_id": identifier, "ats_slug": "acme",
                "url": f"https://acme.pinpointhq.com/postings/{path}", "title": f"Engineer {path}",
                "company": "Acme", "location": "Remote", "description": "Build things. " * 20}

    _, anonymous = store(db, pinpoint(None, "no-id"))
    _, gone = store(db, pinpoint("acme:1", "1"))
    _, listed = store(db, pinpoint("acme:2", "2"))
    db.flush()
    assert _close_vanished(db, {("pinpoint", "acme"): {"acme:2"}}) == 1
    assert gone.closed_at is not None
    assert anonymous.closed_at is None and listed.closed_at is None


def test_a_watched_employers_board_outranks_boards_with_no_company(db):
    from app.models.company import Company
    from app.models.company_board import CompanyBoard
    from app.services.company_boards import board_slugs
    employer = Company(name="Watched Employer", watched=True)
    db.add(employer)
    db.flush()
    for index, slug in enumerate(["unlinked-a", "unlinked-b", "unlinked-c", "watched"]):
        db.add(CompanyBoard(ats="greenhouse", slug=slug, origin="discovered", active=True,
            first_seen_at=NOW, last_seen_at=NOW, last_job_count=0, total_job_count=0,
            # The watched board was polled last, so the coverage reserve
            # (oldest-polled first) does not pick it for its own reason.
            last_fetched_at=NOW - timedelta(hours=10 - index),
            company_id=employer.id if slug == "watched" else None))
    db.flush()
    assert "watched" in board_slugs(db, "greenhouse", 2)


def _board(slug="acme", **overrides):
    from app.models.company_board import CompanyBoard
    return CompanyBoard(**{"ats": "greenhouse", "slug": slug, "origin": "discovered", "active": True,
        "first_seen_at": NOW, "last_seen_at": NOW, "last_job_count": 0, "total_job_count": 0,
        **overrides})


def _run(db):
    from app.models.fetch_run import FetchRun
    run = FetchRun(started_at=NOW, group="boards", status="running")
    db.add(run)
    db.commit()
    return run


def test_a_board_batch_keeps_a_company_name_the_adapter_read(db):
    from app.services.collection_batches import sink_for
    from app.services.sources.base import BoardResult
    from tests.test_collection_reliability import posting
    db.add(_board(company="Acme Corporation"))
    sink = sink_for(db, _run(db).id)
    sink("greenhouse", "acme", BoardResult(jobs=[posting("1", company="Acme Health"),
                                                 posting("2", company="acme")], complete=True))
    assert {job.source_job_id: job.company for job in db.query(Job)} == {"1": "Acme Health", "2": "Acme Corporation"}


def _pending_batch(db, payload, **overrides):
    from app.models.source_listing import FetchBoardRun
    batch = FetchBoardRun(**{"run_id": _run(db).id, "source": "greenhouse", "board": "acme",
        "status": "complete", "observed_at": NOW, "observed_total": len(payload),
        "returned": len(payload), "inserted": 0, "merged": 0, "dropped": 0,
        "payload": payload, **overrides})
    db.add(batch)
    db.commit()
    return batch


def test_a_posting_the_store_rejects_is_not_replayed_forever(db):
    from app.services.collection_batches import replay
    from tests.test_collection_reliability import posting
    batch = _pending_batch(db, [posting("1", title=""), posting("2")])
    assert replay(db) == 2
    db.refresh(batch)
    assert batch.payload is None and batch.dropped == 1
    assert "1 posting(s) could not be stored" in batch.error
    assert replay(db) == 0
    assert db.query(Job).count() == 1


def test_a_posting_that_keeps_failing_stops_being_replayed(db, monkeypatch):
    from app.services import collection_batches
    from tests.test_collection_reliability import posting

    def store(*args, **kwargs):
        raise RuntimeError("deadlock detected")

    monkeypatch.setattr(collection_batches, "store", store)
    batch = _pending_batch(db, [posting("1")])
    for attempt in (1, 2):
        assert collection_batches.replay(db) == 1
        db.refresh(batch)
        assert batch.payload[0]["_ingest_attempts"] == attempt
    assert collection_batches.replay(db) == 1
    db.refresh(batch)
    assert batch.payload is None
    assert collection_batches.replay(db) == 0


def test_replaying_a_failed_remainder_leaves_the_boards_yield_alone(db):
    from app.services.collection_batches import replay
    from tests.test_collection_reliability import posting
    board = _board(last_job_count=50, total_job_count=50)
    db.add(board)
    batch = _pending_batch(db, [posting("1")], status="partial", error_category="ingestion",
                           error="1 posting(s) could not be stored; 1 queued for replay")
    assert replay(db) == 1
    db.refresh(board)
    db.refresh(batch)
    assert batch.payload is None and db.query(Job).count() == 1
    assert board.last_job_count == 50


@pytest.mark.parametrize("status, kept", [(503, True), (429, True), (404, False)])
def test_a_failed_resumed_pass_keeps_its_place_unless_the_place_is_the_problem(db, status, kept):
    import httpx
    from types import SimpleNamespace
    from app.services.company_boards import record_fetch_results
    from app.services.sources.base import collection_results, cycle_settings
    from app.services.sources.smartrecruiters import fetch
    saved = {"offset": 1000}
    db.add(_board(ats="smartrecruiters", fetch_cursor=saved))
    db.flush()
    cfg = SimpleNamespace(SMARTRECRUITERS_MAX_PAGES=3, SMARTRECRUITERS_DETAIL_LIMIT=0, ATS_BOARD_FETCH_WORKERS=1)
    failure = httpx.Response(status, request=httpx.Request("GET", "https://api.smartrecruiters.com"))
    with cycle_settings(cfg), patch("httpx.get", return_value=failure), \
            collection_results(cursors={("smartrecruiters", "acme"): saved}) as results:
        fetch(["acme"])
    result = results[("smartrecruiters", "acme")]
    assert result.error
    record_fetch_results(db, "smartrecruiters", ["acme"], {}, results={"acme": result})
    from app.models.company_board import CompanyBoard
    board = db.query(CompanyBoard).filter_by(ats="smartrecruiters", slug="acme").one()
    assert board.fetch_cursor == (saved if kept else None)


def test_postings_after_an_already_stored_boundary_get_their_own_lookup_and_commit(db, monkeypatch):
    from app.services import collection_ingest, job_fetcher
    _make_profile_with_targets(db)
    legacy = _std_job(title="Legacy", url="https://legacy.example/3", source_job_id="L3")
    cycle(db, [legacy])
    # Stored before rows had an identity key: only the chunk lookup finds it.
    db.query(Job).update({Job.identity_key: None})
    db.commit()

    def stored_by_batch(job):
        return {**job, "_ingested_outcome": "skipped", "_ingested_apply_url": job.get("apply_url"),
                "_ingested_company": job.get("company")}

    jobs = [_std_job(title="New", url="https://legacy.example/0", source_job_id="N0"),
            stored_by_batch(_std_job(title="Batch 1", url="https://legacy.example/1", source_job_id="B1")),
            stored_by_batch(_std_job(title="Batch 2", url="https://legacy.example/2", source_job_id="B2")),
            legacy]
    monkeypatch.setattr(job_fetcher, "_LOOKUP_CHUNK", 2)
    monkeypatch.setattr(job_fetcher, "_COMMIT_EVERY", 2)
    stored, commits = [], []
    original_store, original_commit = collection_ingest.store, db.commit

    def store(*args, **kwargs):
        stored.append(args[1]["url"])
        return original_store(*args, **kwargs)

    def commit():
        commits.append(len(stored))
        original_commit()

    monkeypatch.setattr(collection_ingest, "store", store)
    monkeypatch.setattr(db, "commit", commit)
    cycle(db, jobs)
    assert db.query(Job).filter(Job.url == legacy["url"]).count() == 1
    # The commit due after index 1 ran although that posting was skipped.
    assert 1 in commits


def test_a_board_repeating_its_derived_remote_flag_does_not_undo_extraction(db):
    from app.models.job import JobStatus
    from app.services.collection_ingest import store
    from tests.test_collection_reliability import posting
    _, job = store(db, posting(is_remote=False), now=NOW - timedelta(hours=2))
    # Extraction read "remote" from the posting's JSON-LD and matched it.
    job.is_remote = True
    job.status = JobStatus.matched
    job.details_extracted_at = NOW - timedelta(hours=1)
    db.flush()
    outcome, same = store(db, posting(is_remote=False), now=NOW)
    assert outcome == "skipped"
    assert same.is_remote is True
    assert same.status == JobStatus.matched and same.details_extracted_at is not None


def test_a_board_listing_does_not_reopen_a_job_its_page_says_is_filled(db):
    from app.services.collection_ingest import store
    from app.services.job_fetcher import VANISHED_NOTE
    from tests.test_collection_reliability import posting
    _, filled = store(db, posting("1"), now=NOW - timedelta(hours=2))
    _, unlisted = store(db, posting("2"), now=NOW - timedelta(hours=2))
    filled.closed_at = unlisted.closed_at = NOW - timedelta(hours=1)
    filled.closed_note = 'the page says: "no longer accepting applications"'
    unlisted.closed_note = VANISHED_NOTE
    db.flush()
    store(db, posting("1", _listed_open=True), now=NOW)
    store(db, posting("2", _listed_open=True), now=NOW)
    assert filled.closed_at is not None
    assert unlisted.closed_at is None and unlisted.closed_note is None


def test_a_legacy_row_does_not_take_a_key_another_row_holds(db):
    from app.services import posting_identity
    from app.services.collection_ingest import store
    from tests.test_collection_reliability import posting
    legacy_posting = posting("1")
    _, legacy = store(db, legacy_posting)
    _, other = store(db, posting("2"))
    key = legacy.identity_key
    legacy.identity_key = None
    db.flush()
    other.identity_key = key
    db.flush()
    assert key == posting_identity.job_key("greenhouse", legacy_posting["url"], "1", None, "acme")
    outcome, job = store(db, {**legacy_posting, "salary_min": 120000})
    assert outcome == "merged" and job.id == legacy.id
    assert legacy.identity_key is None and other.identity_key == key


def test_saving_priorities_changes_only_the_employers_this_save_changed(client, db, monkeypatch):
    from app.models.company import Company
    queued = []
    monkeypatch.setattr("app.routers.companies._queue", queued.append)
    client.post("/today/pins", data={"companies": "Paused Employer\nResearched Employer"})
    paused = db.query(Company).filter_by(name="Paused Employer").one()
    researched = db.query(Company).filter_by(name="Researched Employer").one()
    returning = Company(name="Returning Employer", identity_source="user", watched=False)
    db.add(returning)
    paused.watched = False
    researched.research_status, researched.research_note = "ready", "Two Greenhouse boards."
    researched.next_refresh_at = NOW + timedelta(days=3)
    db.commit()
    queued.clear()

    client.post("/today/pins", data={
        "companies": "Paused Employer\nresearched employer\nNew Employer\nReturning Employer"})
    for company in (paused, researched, returning):
        db.refresh(company)
    assert paused.watched is False
    assert researched.research_status == "ready" and researched.next_refresh_at > NOW
    new = db.query(Company).filter_by(name="New Employer").one()
    assert new.watched and returning.watched
    assert queued == [new.id, returning.id]


@pytest.mark.parametrize("rewrite", [False, True])
def test_a_new_conversation_is_committed_before_the_model_is_asked(db, monkeypatch, rewrite):
    """The conversation lock is transaction-scoped: committing is what releases it."""
    from app.models.outreach import OutreachConversation, OutreachMessage
    from app.services import outreach
    from tests.test_outreach_relationships import contact
    person = contact(db)
    db.commit()
    events = []
    original_commit = db.commit

    def commit():
        events.append(("commit", db.query(OutreachConversation).count()))
        original_commit()

    def compose(*args, **kwargs):
        events.append(("compose", None))
        return {"subject": "Platform role", "body": "Hello.", "generated_by": None}

    monkeypatch.setattr(outreach, "compose_message", compose)
    if rewrite:
        draft = OutreachMessage(contact_id=person.id, application_id=person.application_id, status="draft",
                                body="Old", subject="Old", kind="initial", sequence_step=1)
        db.add(draft)
        db.commit()
        monkeypatch.setattr(db, "commit", commit)
        outreach.regenerate_message(db, draft)
    else:
        monkeypatch.setattr(db, "commit", commit)
        outreach.draft_message(db, person)
    assert events.index(("compose", None)) > 0
    assert events[events.index(("compose", None)) - 1] == ("commit", 1)


@pytest.mark.parametrize("state", ["", "draft"])
def test_follow_ups_due_are_listed_beyond_the_first_page_and_any_state_filter(client, db, state):
    from app.models.outreach import OutreachMessage
    from tests.test_outreach_relationships import contact, sent
    person = contact(db)
    due = sent(db, person)
    due.body, due.created_at = "Following up on the platform role", NOW - timedelta(days=9)
    for index in range(50):
        db.add(OutreachMessage(contact_id=person.id, application_id=person.application_id, status="draft",
                               body=f"Newer draft {index}", subject="Draft", kind="initial", sequence_step=2))
    db.commit()
    assert due.follow_up_due_at <= NOW
    page = client.get("/outreach", params={"state": state}).text
    assert "Follow-ups due" in page
    assert "Following up on the platform role" in page


def test_a_reply_recorded_while_smtp_returns_leaves_no_delivery_claim(db):
    """
    Reported as a stuck 'sending' state; it is not one. `mark_replied` releases
    the claim itself, and `send_message` re-reads before deciding. Kept so that
    skipping `mark_sent` for a replied message stays safe.
    """
    from sqlalchemy.orm import sessionmaker
    from app.models.outreach import OutreachMessage
    from app.services import networking, outreach, outreach_sender
    from tests.test_outreach_relationships import contact, draft
    person = contact(db)
    message = draft(db, person)
    networking.conversation_for(db, person)
    db.commit()
    mailbox_session = sessionmaker(bind=db.get_bind(), join_transaction_mode="create_savepoint")

    def deliver(mail):
        with mailbox_session() as other:
            target = other.get(OutreachMessage, message.id)
            assert target.send_state == "sending"
            target.sent_at = target.send_started_at
            outreach.mark_replied(other, target)

    with patch.object(outreach_sender.settings, "OUTREACH_SEND_ENABLED", True), \
         patch.object(outreach_sender.settings, "SMTP_HOST", "smtp.example.com"), \
         patch.object(outreach_sender.settings, "SMTP_FROM_EMAIL", "me@example.com"), \
         patch("app.services.outreach_sender._deliver", side_effect=deliver):
        outreach_sender.send_message(db, message)
    db.refresh(message)
    assert message.status == "replied" and message.send_state == "idle"
    assert not message.delivery_uncertain
