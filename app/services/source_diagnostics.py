"""
Capture why a source returned nothing.

Every adapter handles its own failures: it logs the reason and returns an empty
list. That keeps one broken site from taking down the cycle, but it also means
the orchestrator sees success-with-no-jobs and the UI reports "OK, 0 jobs" for a
source that is in fact being blocked outright. The reason only ever existed in
the container logs.

Rather than change the return type of twenty adapters, this listens: for the
duration of a fetch cycle it captures WARNING and ERROR records from the
`app.services.sources.*` loggers and files each one under the source it came
from. The adapters already write good messages ("Indeed RSS fetch error: 403",
"Dice: page load failed", "Wellfound: no job cards found with any selector") —
they just had nowhere to go.
"""

import logging

# Loggers under this package belong to individual sources; the last component of
# the logger name is the source key used in the fetch stats.
_SOURCE_LOGGER_ROOT = "app.services.sources"

# Keep the UI readable and the profile JSON small.
MAX_MESSAGES_PER_SOURCE = 5
MAX_MESSAGE_LENGTH = 300


class SourceLogCapture(logging.Handler):
    """Collects warnings/errors emitted by source adapters, keyed by source."""

    def __init__(self, level: int = logging.WARNING) -> None:
        super().__init__(level=level)
        self.messages: dict[str, list[str]] = {}

    def emit(self, record: logging.LogRecord) -> None:
        if not record.name.startswith(_SOURCE_LOGGER_ROOT):
            return
        source = record.name.rsplit(".", 1)[-1]
        if source in ("sources", "base"):
            return  # shared helpers, not a source of their own
        bucket = self.messages.setdefault(source, [])
        if len(bucket) >= MAX_MESSAGES_PER_SOURCE:
            return
        try:
            message = record.getMessage()
        except Exception:
            return
        message = message[:MAX_MESSAGE_LENGTH]
        if message not in bucket:
            bucket.append(message)

    def __enter__(self) -> "SourceLogCapture":
        logging.getLogger(_SOURCE_LOGGER_ROOT).addHandler(self)
        return self

    def __exit__(self, *exc_info) -> None:
        logging.getLogger(_SOURCE_LOGGER_ROOT).removeHandler(self)


def merge_into_stats(stats: dict, captured: dict[str, list[str]]) -> None:
    """
    Attach captured reasons to the per-source fetch stats.

    Only for sources that ended up with no jobs: a source that returned results
    despite a warning worked, and surfacing noise there would train the reader
    to ignore the column.
    """
    for source, messages in captured.items():
        entry = stats.get(source)
        if entry is None or entry.get("count"):
            continue
        existing = entry.setdefault("errors", [])
        for message in messages:
            if message not in existing:
                existing.append(message)


def classify(entry: dict) -> str:
    """
    One word for what happened to a source this cycle.

    Distinguishing "found nothing" from "could not look" is the whole point —
    the first is a search that legitimately had no matches, the second is a
    source that needs attention.
    """
    if not entry.get("enabled", True):
        return "disabled"
    count, errors = entry.get("count", 0), entry.get("errors") or []
    if count and errors:
        return "partial"
    if count:
        return "ok"
    return "failed" if errors else "empty"
