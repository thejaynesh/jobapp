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

# Origins we trust without a probe. A slug the user typed and a slug from the
# curated seed list are claims somebody made deliberately; a slug scraped out
# of a link is a guess.
TRUSTED_ORIGINS = frozenset({"configured", "seed"})

AWAITING_VALIDATION = "awaiting validation"


def _validation_enabled() -> bool:
    from app.config import settings

    return bool(getattr(settings, "ATS_BOARD_VALIDATION", True))


def is_blocked_slug(slug: str) -> bool:
    """
    True when this "company slug" is a job board, a tracker, or a URL path.

    The second line of defence: extraction already refuses these, but boards
    also arrive from the legacy profile blob, community lists and the one-time
    backfill, and every one of those paths bypasses extraction. `linkedin`,
    `appcast` and `stepstone` reached the registry through exactly that gap.
    """
    from app.services.ats_discovery import SLUG_BLOCKLIST

    text = (slug or "").strip().lower()
    if not text:
        return True
    # Workday slugs are tenant:host:site — judge the tenant.
    # No length rule: the extraction patterns already require two characters,
    # and a second one here would only reject legitimate short slugs.
    return text.split(":", 1)[0] in SLUG_BLOCKLIST


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
    wanted: set[tuple[str, str]] = set()
    blocked = 0
    for ats, slugs in found.items():
        for raw in (slugs or []):
            slug = (raw or "").strip()
            if not slug:
                continue
            if is_blocked_slug(slug):
                blocked += 1
                continue
            wanted.add((ats, slug))
    if blocked:
        logger.info(
            "company_boards: refused %d slug(s) that name a job board or a URL "
            "path rather than a company", blocked,
        )
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
            # A guessed slug is not polled until something has asked its ATS
            # whether it is real. Slugs the user configured and the curated
            # seed list are claims rather than guesses, so they go straight in
            # — and so does everything, if validation is switched off, because
            # a board held for a probe that will never run is a board lost.
            trusted = origin in TRUSTED_ORIGINS or not _validation_enabled()
            board = CompanyBoard(
                ats=ats,
                slug=slug,
                company=company,
                origin=origin,
                source_host=source_host,
                first_seen_at=now,
                last_seen_at=now,
                active=trusted,
                validated_at=now if trusted else None,
                inactive_reason=None if trusted else AWAITING_VALIDATION,
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
        # Re-seeing a board revives it, but only one that was retired for going
        # quiet. A board held back for validation, or one a probe found not to
        # exist, is not revived by being linked again — it was linked in the
        # first place, and that is exactly the evidence that proved wrong.
        if revive and not board.active and board.validated_at is not None \
                and board.inactive_reason is None:
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


# How different a board's own company name may be from the one we filed it
# under before it counts as the wrong company. Generous: "Stripe" vs
# "Stripe, Inc." is the same employer, and only a wild mismatch is evidence.
_NAME_SIMILARITY_FLOOR = 0.45


def _names_disagree(claimed: str | None, reported: str | None) -> bool:
    if not claimed or not reported:
        return False
    from difflib import SequenceMatcher

    from app.services.deduplication import normalize_company

    left, right = normalize_company(claimed), normalize_company(reported)
    if not left or not right:
        return False
    if left in right or right in left:
        return False
    return SequenceMatcher(None, left, right).ratio() < _NAME_SIMILARITY_FLOOR


def validate_pending(db: Session, limit: int = 150, workers: int = 8) -> dict:
    """
    Probe boards nobody has confirmed exist, and activate the real ones.

    Discovery is a guess by construction: it reads a slug out of a link and
    files it as a company. Most are right; `greenhouse/linkedin`,
    `greenhouse/appcast` and `greenhouse/stepstone` were not, and each was
    polled every cycle for months against a budget real companies were
    competing for.

    Bounded per cycle and concurrent — one cheap request per board, and the
    backlog drains over a few cycles rather than adding minutes to one.
    """
    from concurrent.futures import ThreadPoolExecutor

    from app.services.ats_validation import probe_board

    counts = {"probed": 0, "activated": 0, "rejected": 0, "wrong_company": 0,
              "unreachable": 0}

    pending = (
        db.query(CompanyBoard)
        .filter(CompanyBoard.validated_at.is_(None))
        # Newest first: a board discovered this cycle is the one most likely to
        # be about to matter.
        .order_by(CompanyBoard.first_seen_at.desc())
        .limit(max(0, limit))
        .all()
    )
    if not pending:
        return counts

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(pending)))) as pool:
        results = list(pool.map(
            lambda board: (board, probe_board(board.ats, board.slug)), pending
        ))

    now = datetime.now(timezone.utc)
    for board, probe in results:
        counts["probed"] += 1
        board.validated_at = now

        if not probe.exists:
            board.active = False
            board.inactive_reason = probe.error or "no such board"
            counts["rejected"] += 1
            continue

        if _names_disagree(board.company, probe.company):
            # A live board belonging to somebody else. Kept, because the board
            # is real and worth polling — but refiled under the name its own
            # API gives, so the company column stops lying.
            counts["wrong_company"] += 1
            logger.info(
                "company_boards: %s/%s reports itself as %r, not %r — refiling",
                board.ats, board.slug, probe.company, board.company,
            )
            board.company = probe.company

        if probe.error:
            counts["unreachable"] += 1
        board.active = True
        board.inactive_reason = None
        counts["activated"] += 1

    db.flush()
    logger.info(
        "company_boards: validated %d board(s) — %d activated, %d rejected, "
        "%d refiled under a different company",
        counts["probed"], counts["activated"], counts["rejected"],
        counts["wrong_company"],
    )
    return counts


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
    """
    Per-ATS registry counts for the settings page.

    Inactive is three different things now, and reporting them as one number
    would say "400 boards retired" about a registry where most of those are
    simply waiting for their first probe. Retired means it went quiet;
    `pending` has never been checked; `rejected` was checked and does not
    exist.
    """
    rows = (
        db.query(
            CompanyBoard.ats,
            func.count(CompanyBoard.id),
            func.sum(func.cast(CompanyBoard.active, Integer)),
            func.sum(CompanyBoard.total_job_count),
            func.sum(func.cast(CompanyBoard.validated_at.is_(None), Integer)),
            func.sum(func.cast(
                CompanyBoard.active.is_(False)
                & CompanyBoard.validated_at.isnot(None)
                & CompanyBoard.inactive_reason.isnot(None),
                Integer,
            )),
        )
        .group_by(CompanyBoard.ats)
        .all()
    )
    result = {}
    for ats, total, active, jobs, pending, rejected in rows:
        total, active = int(total or 0), int(active or 0)
        pending, rejected = int(pending or 0), int(rejected or 0)
        result[ats] = {
            "total": total,
            "active": active,
            "pending": pending,
            "rejected": rejected,
            "retired": max(0, total - active - pending - rejected),
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
        .filter(
            CompanyBoard.active.is_(False),
            # A board waiting for its first probe has not been given up on —
            # nobody has looked at it yet, and listing it here as retired would
            # bury the boards that genuinely stopped producing.
            CompanyBoard.validated_at.isnot(None),
        )
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
