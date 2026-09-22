"""
Fetching, in three slices rather than one.

The whole pipeline used to be a single 47-minute task, which meant a source
that could refresh hourly ran on the schedule of the slowest thing beside it:
Adzuna waited behind a Chromium launch, and every posting arrived hours later
than it could have. Each group now has its own task, its own lock and its own
cadence, and each writes its own `fetch_runs` row so a run's numbers are
comparable to the right other runs.

The combined entry point stays, because the manual trigger on `/runs` wants
"everything" and "just this adapter" more often than it wants a group.
"""

import logging

from app.celery_app import celery_app
from app.config import settings
from app.database import SessionLocal
from app.services.fetch_lock import LOCK_KEY, acquire, keepalive, release
from app.services.job_fetcher import ALL_GROUPS, fetch_and_save_jobs

logger = logging.getLogger(__name__)

_EMPTY = {"fetched": 0, "inserted": 0, "merged": 0, "skipped": 0}

# A lock per group.
#
# Groups do not conflict with each other — they touch disjoint sources — so a
# single key would have the hourly API run blocked by the twice-daily browser
# tier, which is most of what this split was for.
#
# Which is what used to happen. Every group run took the combined key as well
# as its own, to keep a manual "fetch everything" from overlapping a scheduled
# group — and since all three contended on that one key, they excluded each
# other too. The per-group keys blocked nothing the combined key had not
# already blocked, and Adzuna went back to waiting behind a Chromium launch.
#
# So the exclusion runs the other way now: a combined run takes *every* group
# key (and the shared one, which is the canonical "a full cycle is happening"
# marker), and a group run takes only its own. A group still blocks "all" and
# "all" still blocks every group; two groups no longer block each other.
GROUP_LOCK_KEYS = {group: f"jobapp:fetch:{group}" for group in ALL_GROUPS}

# Every key that means "some fetch is in flight" — see `fetch_state`.
ALL_LOCK_KEYS = (LOCK_KEY, *GROUP_LOCK_KEYS.values())


def fetch_state() -> dict:
    """
    Whether any fetch is running, for the runs page and the manual trigger.

    Reads every key rather than the combined one, because a scheduled group run
    no longer holds the combined key — and a page that reported "idle" while
    the browser tier was mid-cycle would be telling the user the opposite of
    the truth.
    """
    from app.services.fetch_lock import any_state

    return any_state(ALL_LOCK_KEYS)


def _run(group: str | None, only: list[str] | None, match_after: bool) -> dict:
    """One cycle, under whichever locks this run needs."""
    keys = (
        [LOCK_KEY, *GROUP_LOCK_KEYS.values()]
        if group in (None, "all")
        else [GROUP_LOCK_KEYS[group]]
    )

    held: list[str] = []
    for key in keys:
        if not acquire(key=key):
            for taken in held:
                release(key=taken)
            logger.warning(
                "fetch_jobs(%s): another fetch holds %s; skipping",
                group or "all", key,
            )
            return {**_EMPTY, "skipped_reason": "already running"}
        held.append(key)

    db = SessionLocal()
    try:
        with keepalive(held):
            result = fetch_and_save_jobs(
                db, only=set(only) if only else None, group=group
            )
        logger.info(
            "fetch_jobs(%s) complete — fetched=%d inserted=%d merged=%d skipped=%d",
            group or "all", result["fetched"], result["inserted"],
            result["merged"], result["skipped"],
        )
        if match_after:
            from app.tasks.match import match_jobs
            match_jobs.delay()
        return result
    except Exception as exc:
        logger.error("fetch_jobs(%s) raised unexpectedly: %s", group or "all", exc)
        return dict(_EMPTY)
    finally:
        db.close()
        for key in reversed(held):
            release(key=key)


@celery_app.task(name="app.tasks.fetch.fetch_jobs", bind=True, max_retries=0)
def fetch_jobs(self, only: list[str] | None = None, match_after: bool = True,
               group: str | None = None) -> dict:
    """
    One fetch cycle across every source, or a named subset.

    `only` restricts the run to the named sources — the scheduled groups pass
    nothing and the manual trigger passes what was ticked, which is what makes
    verifying one adapter take seconds instead of minutes.

    Held under a lock so a manual trigger can't overlap a scheduled cycle: two
    at once would double every outbound request and make the per-source numbers
    meaningless.
    """
    return _run(group, only, match_after)


# ---------------------------------------------------------------------------
# The scheduled groups
# ---------------------------------------------------------------------------

# Each scheduled group's interval, as the settings-page key that holds it.
_INTERVAL_KEYS = {
    "api": "fetch_api_interval_hours",
    "boards": "fetch_boards_interval_hours",
    "browser": "fetch_browser_interval_hours",
}

# A run due at 14:00 that the ten-minute tick reaches at 13:58 should go, not
# wait for 14:08 and slip a little further every cycle.
_DUE_SLACK_SECONDS = 300


def _due(group: str) -> bool:
    """
    Whether this group's interval, as currently set, has passed since it ran.

    Read from the profile on every call, so a change on the settings page
    applies to the next tick. Any failure to tell answers "due": a missed run
    is worse than an extra one, and the lock still prevents overlap.
    """
    from datetime import datetime, timezone

    from app.models.fetch_run import FetchRun
    from app.models.profile import Profile
    from app.services.tunables import value as tunable

    db = SessionLocal()
    try:
        profile = db.query(Profile).first()
        hours = tunable(profile.data if profile else {}, _INTERVAL_KEYS[group])
        last = (
            db.query(FetchRun.started_at)
            .filter(FetchRun.group == group)
            .order_by(FetchRun.started_at.desc())
            .limit(1)
            .scalar()
        )
    except Exception as exc:
        logger.warning("fetch: could not tell whether %s is due: %s", group, exc)
        return True
    finally:
        db.close()
    if last is None:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - last).total_seconds()
    return elapsed >= float(hours) * 3600 - _DUE_SLACK_SECONDS


def _scheduled(group: str) -> dict:
    """A scheduled group run, if its interval has passed."""
    try:
        from app.services.fetch_lock import _client

        _client().delete(f"jobapp:fetch:queued:{group}")
    except Exception:
        pass
    if not _due(group):
        return {**_EMPTY, "skipped_reason": "not due"}
    return _run(group, None, True)


@celery_app.task(name="app.tasks.fetch.dispatch_due_fetches", bind=False, max_retries=0)
def dispatch_due_fetches() -> list[str]:
    """
    Queue every fetch group whose interval has passed and is not running.

    Beat calls this on a short tick instead of scheduling each group itself,
    because beat reads its schedule once at start — the intervals on the
    settings page could not have reached it.
    """
    from app.services.fetch_lock import any_state

    tasks = {"api": fetch_api_sources, "boards": fetch_ats_boards,
             "browser": fetch_browser_tier}
    queued = []
    for group, task in tasks.items():
        if group == "browser" and not settings.BROWSER_TIER_ENABLED:
            continue
        try:
            if any_state((GROUP_LOCK_KEYS[group], LOCK_KEY)).get("running"):
                continue
        except Exception:
            pass
        if not _due(group):
            continue
        # One queued copy at a time. The batch worker can be busy for hours,
        # and a tick every ten minutes would otherwise stack a copy per tick
        # behind it — each harmless (it finds the group not due and returns),
        # but a queue full of no-ops hides the work that is actually waiting.
        try:
            from app.services.fetch_lock import _client

            if not _client().set(f"jobapp:fetch:queued:{group}", "1",
                                 nx=True, ex=_QUEUED_MARKER_SECONDS):
                continue
        except Exception:
            pass
        task.delay()
        queued.append(group)
    return queued


# Long enough to cover a busy worker, short enough that a lost task (a worker
# killed before it ran) is re-queued within the hour.
_QUEUED_MARKER_SECONDS = 3600


@celery_app.task(name="app.tasks.fetch.fetch_api_sources", bind=False, max_retries=0)
def fetch_api_sources() -> dict:
    """The cheap tier: keyed APIs and public feeds. Minutes, so run it often."""
    return _scheduled("api")


@celery_app.task(name="app.tasks.fetch.fetch_ats_boards", bind=False, max_retries=0)
def fetch_ats_boards() -> dict:
    """The company board registry: hundreds of slugs, one request each."""
    return _scheduled("boards")


@celery_app.task(name="app.tasks.fetch.fetch_browser_tier", bind=False, max_retries=0)
def fetch_browser_tier() -> dict:
    """Playwright. The most expensive thing here, and the least urgent."""
    if not settings.BROWSER_TIER_ENABLED:
        return {**_EMPTY, "skipped_reason": "disabled"}
    return _scheduled("browser")


@celery_app.task(name="app.tasks.fetch.sweep_linked_boards", bind=False, max_retries=0)
def sweep_linked_boards(deep: bool = False) -> dict:
    """
    Boards that have to be asked over their own API, with a stored credential.

    `deep` picks which of Tsenta's two lists to read. The default is their
    recommendation feed — 216 postings, eleven round trips — which is what the
    site itself shows and is worth re-reading every few hours. The deep sweep
    reads their whole index, which the offset cap will not let anyone page
    through in one query, so it goes state by state: about a thousand requests
    for roughly 1,845 postings, which is a daily job rather than a three-hourly
    one.

    Separate from the three tiers above because it is a different kind of
    source. Those adapters need nothing but a URL and a key from the
    environment; this one needs a credential a person had to be signed in to
    obtain, which means it can be *unlinked* — a state the fetch cycle has no
    vocabulary for and should not learn one for.

    It also fails differently. A board here does not break, it expires: the
    refresh token dies when the user signs out everywhere or changes a
    password, and the repair is to open the site in a browser once, which
    re-links automatically. So a failure is recorded and reported rather than
    retried, because retrying a dead credential faster does not revive it.
    """
    from app.services import agent_events
    from app.services.sources import tsenta

    db = SessionLocal()
    results: dict[str, dict] = {}
    try:
        outcome = tsenta.sweep(db, deep=deep)
        results[tsenta.SITE] = outcome
        try:
            # The same event kind the extension's sweep reports under, so the
            # panel shows both and a board that moved from one path to the
            # other stays comparable across the move.
            agent_events.record(
                db, "sweep", url=tsenta.API, agent_id="server",
                ok=bool(outcome["pages"]),
                summary={
                    "pages": outcome["pages"], "rows": outcome["rows"],
                    "limit": outcome["limit"], "stopped": outcome["stopped"],
                    "detail": outcome["detail"], "status": 0,
                    "inserted": outcome["inserted"], "merged": outcome["merged"],
                    # Coverage, not throughput: a capped slice is postings we
                    # know exist and cannot reach with this partition.
                    "slices": outcome["slices"],
                    "capped_slices": outcome["capped_slices"],
                    "deep": outcome["deep"],
                },
            )
            db.commit()
        except Exception as exc:
            logger.warning("sweep_linked_boards: could not record the sweep: %s", exc)
    except Exception as exc:
        logger.error("sweep_linked_boards: %s", exc)
        results["error"] = {"detail": str(exc)[:200]}
    finally:
        db.close()
    return results
