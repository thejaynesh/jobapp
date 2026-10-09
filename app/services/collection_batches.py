"""Commit completed network batches before ingestion; replay interrupted ones."""
import json
import logging
import threading
from datetime import datetime, timezone

from sqlalchemy.orm import sessionmaker
from sqlalchemy.engine import Connection

from app.models.company_board import CompanyBoard
from app.models.source_listing import FetchBoardRun
from app.services.collection_ingest import store

logger = logging.getLogger(__name__)


def _consume(db, batch, jobs, *, max_age_days=0, result=None):
    linked = []
    failed = []
    outcomes = []
    from app.services.job_fetcher import _known_postings
    known = _known_postings(db, jobs)
    names = {b.slug: b.company for b in db.query(CompanyBoard).filter(
        CompanyBoard.ats == batch.source, CompanyBoard.company.isnot(None))} if batch.board else {}
    from app.services.posting_identity import job_key
    for data in sorted(jobs, key=lambda j: job_key(j.get("source") or "", j.get("url") or "",
                        j.get("source_job_id"), j.get("apply_url"), j.get("ats_slug"))):
        if names.get(data.get("ats_slug")):
            data["company"] = names[data["ats_slug"]]
        try:
            outcome, job = store(db, data, max_age_days=max_age_days, known=known, now=batch.observed_at)
            outcomes.append((data, outcome))
            if job is not None:
                linked.append(job)
            if outcome in ("inserted", "merged"):
                setattr(batch, outcome, (getattr(batch, outcome) or 0) + 1)
        except Exception as exc:
            batch.dropped = (batch.dropped or 0) + 1
            failed.append(data)
            logger.warning("collection: could not ingest %s: %s", data.get("url"), exc)
    if linked:
        from app.services.company_identity import attach_known_companies
        attach_known_companies(db, linked)
    # Only failed rows remain replayable. Successful inserts and this state
    # transition are committed together, including during crash recovery.
    batch.payload = json.loads(json.dumps(failed, default=str)) if failed else None
    if failed:
        batch.error = f"{len(failed)} posting(s) could not be stored; queued for replay"
        batch.error_category = "ingestion"
        batch.status = "partial" if linked else "failed"
    if batch.board:
        from app.services.company_boards import record_fetch_results
        from app.services.sources.base import BoardResult
        result = result or BoardResult(jobs=jobs, complete=True if batch.status == "complete" else None,
            total=batch.observed_total, cursor=batch.cursor,
            error=batch.error if batch.error_category != "ingestion" else None,
            error_category=batch.error_category if batch.error_category != "ingestion" else None)
        result.inserted, result.merged, result.dropped = batch.inserted, batch.merged, batch.dropped
        record_fetch_results(db, batch.source, [batch.board], {batch.board: len(jobs)},
                             results={batch.board: result}, observed_at=batch.observed_at)
    db.commit()
    if result is not None:
        result.recorded = True
    for data, outcome in outcomes:
        data["_ingested_outcome"] = outcome
        data["_ingested_apply_url"] = data.get("apply_url")
        data["_ingested_company"] = data.get("company")


def replay(db, *, max_age_days=0) -> int:
    """Replay pending batches once, with row locks so two groups cannot race."""
    count = 0
    attempted = []
    for _ in range(100):
        # Reacquire one lease after each commit. Selecting 100 locks at once
        # would release 99 of them when the first batch commits.
        batch = db.query(FetchBoardRun).filter(FetchBoardRun.has_pending_payload(),
            FetchBoardRun.id.notin_(attempted)).order_by(FetchBoardRun.observed_at).with_for_update(
                skip_locked=True).first()
        if batch is None:
            break
        attempted.append(batch.id)
        jobs = list(batch.payload or [])
        _consume(db, batch, jobs, max_age_days=max_age_days)
        count += len(jobs)
    return count


def sink_for(db, run_id, *, max_age_days=0):
    # Every network thread owns a short-lived session; the calling thread's
    # long-running Session is never shared with workers.
    bind = db.get_bind()
    factory = sessionmaker(bind=bind, expire_on_commit=False,
        **({"join_transaction_mode": "create_savepoint"} if isinstance(bind, Connection) else {}))
    # Bounded database backpressure: dozens of network workers do not each
    # claim a DB connection. A test's externally owned Connection is likewise
    # never used by two threads at once.
    writer = threading.Lock()

    def sink(source, board, result):
        jobs = [j for j in result.jobs if not j.get("_ingested_outcome")]
        if result.jobs and not jobs:
            return
        with writer, factory() as session:
            batch = FetchBoardRun(run_id=run_id, source=source, board=board,
                status=result.status, observed_at=datetime.now(timezone.utc), observed_total=result.total,
                returned=len(jobs), inserted=0, merged=0, dropped=0, cursor=result.cursor,
                error_category=result.error_category, error=result.error,
                payload=json.loads(json.dumps(jobs, default=str)) if jobs else None)
            session.add(batch)
            session.commit()
            _consume(session, batch, jobs, max_age_days=max_age_days, result=result)
            result.inserted, result.merged, result.dropped = batch.inserted, batch.merged, batch.dropped
    return sink
