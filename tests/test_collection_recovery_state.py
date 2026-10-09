"""Cleared JSON state must not mask, replay, or retain completed batches."""
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import JSON, text

from app.models.company_board import CompanyBoard
from app.models.fetch_run import FetchRun
from app.models.job import Job
from app.models.source_listing import FetchBoardRun, has_resume_cursor
from app.services import collection_batches
from app.services.fetch_history import prune
from tests.test_collection_reliability import posting


NOW = datetime.now(timezone.utc)


def _run(db, *, started_at=NOW, finished=True):
    run = FetchRun(started_at=started_at, group="boards", status="ok" if finished else "running",
                   finished_at=NOW if finished else None)
    db.add(run)
    db.flush()
    return run


def _batch(run, *, payload=None, cursor=None, board="", observed_at=NOW):
    return FetchBoardRun(run_id=run.id, source="greenhouse", board=board,
        status="complete", observed_at=observed_at, returned=1,
        payload=payload, cursor=cursor)


def test_none_is_stored_as_sql_null_and_only_real_work_matches(db):
    run = _run(db)
    cases = [
        ("sql-null", None, None),
        ("json-null", JSON.NULL, JSON.NULL),
        ("empty", [], {}),
        ("wrong-shape", {"unexpected": True}, []),
        ("scalar", 0, "next-page"),
        ("pending", [posting()], {"offset": 100}),
    ]
    db.add_all(_batch(run, board=name, payload=payload, cursor=cursor)
               for name, payload, cursor in cases)
    board = CompanyBoard(ats="greenhouse", slug="acme", first_seen_at=NOW,
                         last_seen_at=NOW, fetch_cursor=None)
    db.add(board)
    db.commit()

    # Python reads both null representations as None; only SQL can tell us
    # whether writing None actually cleared the recovery/cursor columns.
    assert db.query(FetchBoardRun).filter(FetchBoardRun.payload.is_(None)).one().board == "sql-null"
    assert db.query(FetchBoardRun).filter(FetchBoardRun.cursor.is_(None)).one().board == "sql-null"
    assert db.query(CompanyBoard).filter(CompanyBoard.fetch_cursor.is_(None)).one().id == board.id
    assert [row.board for row in db.query(FetchBoardRun).filter(
        FetchBoardRun.has_pending_payload())] == ["pending"]
    assert [row.board for row in db.query(FetchBoardRun).filter(
        has_resume_cursor(FetchBoardRun.cursor))] == ["pending"]
    flags = db.query(FetchBoardRun.board,
        FetchBoardRun.has_pending_payload().label("pending"),
        has_resume_cursor(FetchBoardRun.cursor).label("cursor")).all()
    assert all(row.pending is False and row.cursor is False for row in flags if row.board != "pending")


def test_cleared_batches_do_not_exhaust_the_recovery_limit(db):
    run = _run(db)
    # More than the replay budget, inserted in one cheap flush. Legacy JSON
    # nulls and empty arrays must not hide the real batch behind them.
    empty_states = (None, JSON.NULL, [])
    db.add_all(_batch(run, payload=empty_states[i % 3],
                      observed_at=NOW - timedelta(minutes=2)) for i in range(105))
    pending = _batch(run, payload=[posting()])
    db.add(pending)
    db.commit()
    pending_id = pending.id

    with patch.object(collection_batches, "_consume", wraps=collection_batches._consume) as consume:
        assert collection_batches.replay(db) == 1
        assert collection_batches.replay(db) == 0
        assert consume.call_count == 1
    assert db.query(Job).count() == 1
    assert db.query(FetchBoardRun).filter(FetchBoardRun.id == pending_id,
        FetchBoardRun.payload.is_(None)).count() == 1
    assert db.query(FetchBoardRun).filter(FetchBoardRun.has_pending_payload()).count() == 0


def test_job_changes_and_payload_clear_commit_together(db, monkeypatch):
    run = _run(db)
    batch = _batch(run, payload=[posting()])
    db.add(batch)
    db.commit()
    batch_id = batch.id

    def failed_commit():
        raise RuntimeError("lost connection before commit")

    with monkeypatch.context() as patches:
        patches.setattr(db, "commit", failed_commit)
        with pytest.raises(RuntimeError, match="before commit"):
            collection_batches.replay(db)
    db.rollback()
    assert db.query(Job).count() == 0
    assert db.get(FetchBoardRun, batch_id).payload == [posting()]
    assert db.query(FetchBoardRun).filter(FetchBoardRun.has_pending_payload()).count() == 1

    assert collection_batches.replay(db) == 1
    assert db.query(Job).count() == 1
    assert db.query(FetchBoardRun).filter(FetchBoardRun.id == batch_id,
        FetchBoardRun.payload.is_(None)).count() == 1


def test_retention_preserves_real_pending_work_but_not_cleared_json(db):
    cleared_ids = []
    for i, payload in enumerate((None, JSON.NULL, [])):
        run = _run(db, started_at=NOW - timedelta(hours=10 + i))
        cleared_ids.append(run.id)
        db.add(_batch(run, payload=payload))
    pending = _run(db, started_at=NOW - timedelta(hours=9))
    db.add(_batch(pending, payload=[posting()]))
    unfinished = _run(db, started_at=NOW - timedelta(hours=8), finished=False)
    newest = _run(db)
    kept_ids = {pending.id, unfinished.id, newest.id}
    db.commit()

    assert prune(db, retention=1) == len(cleared_ids)
    db.commit()
    assert {row.id for row in db.query(FetchRun)} == kept_ids
    assert db.query(FetchBoardRun).count() == 1


def test_migration_normalizes_only_json_null_and_keeps_saved_work(db):
    path = Path(__file__).resolve().parents[1] / "alembic/versions/0062_collection_null_state.py"
    spec = importlib.util.spec_from_file_location("collection_null_state", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    run = _run(db)
    rows = [
        _batch(run, board="legacy", payload=JSON.NULL, cursor=JSON.NULL),
        _batch(run, board="absent"),
        _batch(run, board="empty", payload=[], cursor={}),
        _batch(run, board="pending", payload=[posting()], cursor={"offset": 100}),
    ]
    db.add_all(rows)
    for slug, cursor in (("legacy", JSON.NULL), ("empty", {}), ("pending", {"offset": 100})):
        db.add(CompanyBoard(ats="greenhouse", slug=slug, first_seen_at=NOW,
                           last_seen_at=NOW, fetch_cursor=cursor))
    db.commit()

    with patch.object(migration.op, "execute", side_effect=lambda sql: db.execute(text(sql))):
        migration.upgrade()
    db.commit()
    db.expire_all()
    assert {row.board for row in db.query(FetchBoardRun).filter(
        FetchBoardRun.payload.is_(None), FetchBoardRun.cursor.is_(None))} == {"legacy", "absent"}
    saved = {row.board: row for row in db.query(FetchBoardRun)}
    assert saved["empty"].payload == [] and saved["empty"].cursor == {}
    assert saved["pending"].payload == [posting()]
    assert saved["pending"].cursor == {"offset": 100}
    assert db.query(CompanyBoard).filter(CompanyBoard.fetch_cursor.is_(None)).one().slug == "legacy"
    boards = {row.slug: row for row in db.query(CompanyBoard)}
    assert boards["empty"].fetch_cursor == {}
    assert boards["pending"].fetch_cursor == {"offset": 100}
