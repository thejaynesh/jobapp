"""Collection status must describe queued work, not JSON null sentinels."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import JSON

from app.models.company_board import CompanyBoard
from app.models.fetch_run import FetchRun
from app.models.source_listing import FetchBoardRun
from app.routers.runs import _collection_context, templates


def test_coverage_only_labels_real_recovery_and_resume_work(db):
    now = datetime.now(timezone.utc)
    run = FetchRun(started_at=now, group="boards", status="ok")
    db.add(run)
    db.flush()
    cases = [
        ("sql-null", None, None),
        ("legacy-json-null", JSON.NULL, JSON.NULL),
        ("empty", [], {}),
        ("wrong-types", {}, []),
        ("pending", [{"url": "https://example.test/jobs/1"}], {"page": 1}),
    ]
    for board, payload, cursor in cases:
        db.add(FetchBoardRun(run_id=run.id, source="rippling", board=board,
            status="partial" if board == "pending" else "unknown", observed_at=now,
            returned=29, inserted=0, merged=2, payload=payload, cursor=cursor))
    db.commit()

    context = _collection_context(db)
    assert context["recovery_pending"] == 1
    rows = {row.board: row for row in context["recent"]}
    for board, _, _ in cases:
        assert rows[board].recovery_pending is (board == "pending")
        assert rows[board].has_cursor is (board == "pending")

    html = templates.env.get_template("runs/partials/collection.html").render(
        system={"collection": context})
    assert "1 batches awaiting recovery" in html
    assert html.count("Recovery pending") == 1
    assert html.count("More pages to collect") == 1
    assert "Coverage unverified" in html
    assert "Saved counts include only new and updated jobs" in html


def test_boards_due_counts_active_eligible_boards_not_future_or_retired(db):
    now = datetime.now(timezone.utc)
    for slug, active, due in [
        ("never-fetched", True, None),
        ("overdue", True, now - timedelta(hours=1)),
        ("later", True, now + timedelta(hours=1)),
        ("retired", False, None),
    ]:
        db.add(CompanyBoard(ats="rippling", slug=slug, active=active,
            next_due_at=due, first_seen_at=now, last_seen_at=now))
    db.commit()
    assert _collection_context(db)["boards_due"] == 2
