"""
Showing a stored instant in the timezone the person reading it lives in.

Timestamps are stored as timezone-aware UTC, which is the only thing worth
storing: it sorts, it compares, and it does not move twice a year. What it is
not is readable. A fetch cycle that ran at half past four in the afternoon
printed as `Aug 18 23:53`, and every page that showed a time asked the reader
to do arithmetic.

Two rules here, both about not making the underlying data worse:

* **It never raises.** A page that cannot render one timestamp should show that
  timestamp badly, not fail. A malformed value, an unknown zone name, a `None`
  — each degrades to something printable.
* **It never touches storage.** This is a rendering concern. Nothing that
  compares, sorts, expires or schedules goes through here, so a change of
  display zone cannot alter what the pipeline actually does.
"""

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import settings

logger = logging.getLogger(__name__)

DEFAULT_ZONE = "America/Los_Angeles"

# Resolving a zone reads and parses the IANA database, which is not free and is
# done on every timestamp on a page that shows hundreds.
_cache: dict[str, ZoneInfo] = {}


def zone() -> ZoneInfo:
    """The configured display zone, falling back to UTC if it cannot be read."""
    name = str(getattr(settings, "DISPLAY_TIMEZONE", "") or DEFAULT_ZONE).strip()
    if name in _cache:
        return _cache[name]
    try:
        found = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        # A typo in a setting, or an image with no tzdata. Saying so once beats
        # every timestamp on every page being quietly wrong.
        logger.warning(
            "timefmt: DISPLAY_TIMEZONE=%r could not be loaded (%s) — showing UTC",
            name, exc,
        )
        found = ZoneInfo("UTC") if _utc_available() else timezone.utc  # type: ignore[assignment]
    _cache[name] = found  # type: ignore[assignment]
    return found  # type: ignore[return-value]


def _utc_available() -> bool:
    try:
        ZoneInfo("UTC")
        return True
    except Exception:
        return False


def local_time(value):
    """
    The same instant in the display zone. Passes anything unusable straight
    through, so a caller can chain this without checking first.
    """
    if not isinstance(value, datetime):
        return value
    # A naive datetime is UTC by convention here: every column in this schema is
    # `DateTime(timezone=True)`, and the ones that arrive naive came from a
    # hand-built object rather than the database.
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    try:
        return value.astimezone(zone())
    except (ValueError, OverflowError, OSError):
        return value


def when(value, pattern: str = "%b %d %H:%M") -> str:
    """
    A stored instant, rendered where the reader lives.

    The template filter. `{{ run.started_at | when("%b %d %H:%M") }}` replaces
    `{{ run.started_at.strftime("%b %d %H:%M") }}` and differs in two ways: it
    converts to the display zone, and it survives a `None` rather than raising
    `'NoneType' has no attribute 'strftime'` and taking the page down.
    """
    moment = local_time(value)
    if not isinstance(moment, datetime):
        return "" if moment is None else str(moment)
    try:
        return moment.strftime(pattern)
    except (ValueError, TypeError):
        return moment.isoformat(sep=" ", timespec="minutes")


def on(value, pattern: str = "%b %d, %Y") -> str:
    """
    A calendar date as the source stated it, with no conversion.

    Sources report a posting date, not an instant: "1 August" arrives as
    `2026-08-01T00:00:00Z` because a timestamp is the only column there is.
    Converting that to Pacific renders it as 31 July — which is not a timezone
    being helpful, it is a date being wrong, and it would make the date sort on
    the jobs page look broken for every posting reported at midnight.

    So dates stay as stored and instants convert. `when` is for the second
    kind: a fetch, a run, a score, a backup — things that happened at a moment
    and are worth reading on your own clock.
    """
    if not isinstance(value, datetime):
        return "" if value is None else str(value)
    try:
        return value.strftime(pattern)
    except (ValueError, TypeError):
        return value.isoformat(sep=" ", timespec="minutes")


def label() -> str:
    """
    What to call the zone in a page footer — "PDT", "PST", "UTC".

    Read from the current moment rather than the name, because the whole reason
    to store a zone rather than an offset is that the abbreviation changes.
    """
    try:
        return datetime.now(zone()).strftime("%Z") or "UTC"
    except Exception:
        return "UTC"
