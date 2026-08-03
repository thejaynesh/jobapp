"""
The company → ATS board registry.

Every ATS board we ever learn about lands here: configured slugs, the verified
seed list, links mined out of fetched postings, community job lists, apply URLs
resolved from aggregator redirects, and boards sniffed off careers sites. Each
cycle the fetcher asks the registry which boards to poll, then reports back how
many jobs each one produced.

That feedback loop is the point. The old flat JSON list was capped at 100 slugs
per ATS and dropped newest-first, so a productive board discovered late simply
never got polled. Here, boards are ranked by what they actually yield and dead
ones are retired, so the per-cycle budget keeps going to companies that hire.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import Integer, func
from sqlalchemy.orm import Session

from app.models.company_board import CompanyBoard

logger = logging.getLogger(__name__)

# Origins the user chose deliberately — never auto-retire these, however quiet.
PROTECTED_ORIGINS = frozenset({"configured", "seed"})

# Consecutive empty cycles before an auto-discovered board is retired. Generous
# on purpose: a transient API error looks identical to an empty board here.
DEFAULT_MAX_EMPTY_CYCLES = 8


def record_boards(
    db: Session,
    found: dict[str, list[str] | set[str]],
    origin: str,
    company: str | None = None,
    source_host: str | None = None,
    revive: bool = True,
) -> int:
    """
    Upsert `{ats: [slug, ...]}` into the registry. Returns the number of boards
    seen for the first time.

    A board freshly linked from a posting is evidence the company is still
    hiring, so re-seeing one refreshes `last_seen_at` and revives it if it had
    been retired. Pass `revive=False` when replaying a stored list (seeds, the
    legacy profile blob) — those say nothing new each cycle, and reviving from
    them would resurrect every retired board forever.
    """
    wanted: set[tuple[str, str]] = {
        (ats, slug.strip())
        for ats, slugs in found.items()
        for slug in (slugs or [])
        if slug and slug.strip()
    }
    if not wanted:
        return 0

    now = datetime.now(timezone.utc)

    # One query for the whole batch, then an in-memory index — a slug repeated
    # within the batch (seeds and discovery routinely overlap) must resolve to
    # the row we just added, not to a second INSERT that trips the unique key.
    existing = (
        db.query(CompanyBoard)
        .filter(CompanyBoard.ats.in_({ats for ats, _ in wanted}))
        .all()
    )
    index = {(board.ats, board.slug): board for board in existing}

    new_count = 0
    for ats, slug in sorted(wanted):
        board = index.get((ats, slug))
        if board is None:
            board = CompanyBoard(
                ats=ats,
                slug=slug,
                company=company,
                origin=origin,
                source_host=source_host,
                first_seen_at=now,
                last_seen_at=now,
            )
            db.add(board)
            index[(ats, slug)] = board
            new_count += 1
            continue

        board.last_seen_at = now
        if company and not board.company:
            board.company = company
        if source_host and not board.source_host:
            board.source_host = source_host
        if revive and not board.active:
            board.active = True
            board.consecutive_empty = 0

    # Make the new rows visible to the next caller's query.
    db.flush()

    if new_count:
        logger.info("company_boards: %d new boards from %s", new_count, origin)
    return new_count


def board_slugs(db: Session, ats: str, limit: int) -> list[str]:
    """
    Active boards for one ATS, best first: proven producers, then the most
    recently seen. Ties broken by slug so the order is stable across cycles.
    """
    rows = (
        db.query(CompanyBoard.slug)
        .filter(CompanyBoard.ats == ats, CompanyBoard.active.is_(True))
        .order_by(
            CompanyBoard.last_job_count.desc(),
            CompanyBoard.total_job_count.desc(),
            CompanyBoard.last_seen_at.desc(),
            CompanyBoard.slug.asc(),
        )
        .limit(limit)
        .all()
    )
    return [row[0] for row in rows]


def registry_slugs(db: Session, caps: dict[str, int]) -> dict[str, list[str]]:
    """Ranked slugs per ATS, each capped by `caps[ats]`."""
    return {ats: board_slugs(db, ats, cap) for ats, cap in caps.items()}


def record_fetch_results(
    db: Session,
    ats: str,
    attempted: list[str],
    counts: dict[str, int],
    had_errors: bool = False,
    max_empty_cycles: int = DEFAULT_MAX_EMPTY_CYCLES,
) -> None:
    """
    Record what each polled board returned this cycle.

    `counts` maps slug → jobs returned; slugs in `attempted` but absent from
    `counts` returned nothing. A board that errored is indistinguishable from an
    empty one here, and deliberately so: a dead slug 404s every cycle and should
    retire, while a transient failure is a single tick that the next good cycle
    resets. `had_errors` covers the case we *can* tell apart — a whole-ATS
    outage — where nothing should be counted against any board.
    """
    if not attempted:
        return

    now = datetime.now(timezone.utc)
    boards = (
        db.query(CompanyBoard)
        .filter(CompanyBoard.ats == ats, CompanyBoard.slug.in_(attempted))
        .all()
    )
    retired = 0
    for board in boards:
        count = counts.get(board.slug, 0)
        board.last_fetched_at = now
        board.last_job_count = count
        if count:
            board.total_job_count += count
            board.consecutive_empty = 0
            continue
        if had_errors:
            continue
        board.consecutive_empty += 1
        if (
            board.consecutive_empty >= max_empty_cycles
            and board.origin not in PROTECTED_ORIGINS
        ):
            board.active = False
            retired += 1

    if retired:
        logger.info("company_boards: retired %d silent %s boards", retired, ats)


def backfill_from_slugs(
    db: Session, slugs_by_ats: dict[str, list[str]], origin: str
) -> int:
    """Import a stored slug mapping (config, seeds, the legacy profile blob)."""
    return record_boards(db, slugs_by_ats, origin=origin, revive=False)


def summary(db: Session) -> dict:
    """Per-ATS registry counts for the settings page."""
    rows = (
        db.query(
            CompanyBoard.ats,
            func.count(CompanyBoard.id),
            func.sum(func.cast(CompanyBoard.active, Integer)),
            func.sum(CompanyBoard.total_job_count),
        )
        .group_by(CompanyBoard.ats)
        .all()
    )
    result = {}
    for ats, total, active, jobs in rows:
        total, active = int(total or 0), int(active or 0)
        result[ats] = {
            "total": total,
            "active": active,
            "retired": total - active,
            "jobs": int(jobs or 0),
        }
    return result


def retired_boards(db: Session, limit: int = 200) -> list[CompanyBoard]:
    """
    Boards we've stopped polling, most recently given up on first.

    Retirement is silent by design — it just stops costing us requests — but
    that also makes it invisible, and a board going quiet is often a signal
    worth acting on: a renamed slug, a company that stopped hiring, a board
    that moved to a different ATS. Surfacing these lets that be a decision
    rather than something that quietly happens.
    """
    return (
        db.query(CompanyBoard)
        .filter(CompanyBoard.active.is_(False))
        .order_by(
            CompanyBoard.last_fetched_at.desc().nullslast(),
            CompanyBoard.ats.asc(),
            CompanyBoard.slug.asc(),
        )
        .limit(limit)
        .all()
    )


def reactivate(db: Session, board_id) -> CompanyBoard | None:
    """Put a retired board back into rotation with a clean slate."""
    board = db.query(CompanyBoard).filter(CompanyBoard.id == board_id).first()
    if board is None:
        return None
    board.active = True
    board.consecutive_empty = 0
    logger.info("company_boards: reactivated %s/%s", board.ats, board.slug)
    return board
