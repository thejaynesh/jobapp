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
from app.services.fetch_lock import LOCK_KEY, acquire, release
from app.services.job_fetcher import ALL_GROUPS, fetch_and_save_jobs

logger = logging.getLogger(__name__)

_EMPTY = {"fetched": 0, "inserted": 0, "merged": 0, "skipped": 0}

# A lock per group, plus the shared one.
#
# Groups do not conflict with each other — they touch disjoint sources — so a
# single key would have the hourly API run blocked by the twice-daily browser
# tier, which is most of what this split was for. They all take the combined
# key as well, so a manual "fetch everything" and a scheduled group still
# cannot overlap.
GROUP_LOCK_KEYS = {group: f"jobapp:fetch:{group}" for group in ALL_GROUPS}


def _run(group: str | None, only: list[str] | None, match_after: bool) -> dict:
    """One cycle, under whichever locks this run needs."""
    keys = [LOCK_KEY] if group in (None, "all") else [GROUP_LOCK_KEYS[group], LOCK_KEY]

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

@celery_app.task(name="app.tasks.fetch.fetch_api_sources", bind=False, max_retries=0)
def fetch_api_sources() -> dict:
    """The cheap tier: keyed APIs and public feeds. Minutes, so run it often."""
    return _run("api", None, True)


@celery_app.task(name="app.tasks.fetch.fetch_ats_boards", bind=False, max_retries=0)
def fetch_ats_boards() -> dict:
    """The company board registry: hundreds of slugs, one request each."""
    return _run("boards", None, True)


@celery_app.task(name="app.tasks.fetch.fetch_browser_tier", bind=False, max_retries=0)
def fetch_browser_tier() -> dict:
    """Playwright. The most expensive thing here, and the least urgent."""
    if not settings.BROWSER_TIER_ENABLED:
        return {**_EMPTY, "skipped_reason": "disabled"}
    return _run("browser", None, True)


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
