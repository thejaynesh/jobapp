"""
In-memory ring buffer of recent system events, surfaced on the web UI.

Every module already logs through Python's stdlib logging. The problem is
that those lines land in a container's stdout, which nobody reads unless
something is already visibly wrong — and by then the lines that would have
said so are off the top of the scroll. This captures the interesting ones
(WARNING and above, plus selected INFO) and keeps the last N in memory so
the web UI can show them without tailing a log file.

The buffer is deliberately in-memory rather than in the database: it is a
view into what the process is doing *right now*, not a durable audit trail.
A restart clears it, which is the right thing — stale events from a
previous boot would be noise.
"""

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone


MAX_EVENTS = 200

CATEGORY_MAP = {
    "app.services.sources": "fetch",
    "app.services.job_fetcher": "fetch",
    "app.services.fetch_history": "fetch",
    "app.services.fetch_lock": "fetch",
    "app.services.matcher": "match",
    "app.services.match_eval": "match",
    "app.services.screening": "match",
    "app.services.eligibility": "match",
    "app.services.doc_generator": "generate",
    "app.services.doc_refresh": "generate",
    "app.services.self_review": "generate",
    "app.services.enrichment": "enrich",
    "app.services.enrichment_history": "enrich",
    "app.services.descriptions": "enrich",
    "app.services.browser_tasks": "agent",
    "app.services.browse_plan": "agent",
    "app.services.agent_events": "agent",
    "app.services.agent_work": "agent",
    "app.services.harvest": "agent",
    "app.services.harvest_recipes": "agent",
    "app.services.harvest_samples": "agent",
    "app.services.provider_check": "provider",
    "app.services.llm_log": "provider",
    "app.services.llm_gate": "provider",
    "app.llm": "provider",
    "app.services.outreach": "outreach",
    "app.services.outreach_sender": "outreach",
    "app.services.contact_finder": "outreach",
    "app.services.mailbox": "outreach",
    "app.services.backups": "system",
    "app.services.pipeline": "system",
    "app.services.liveness": "system",
    "app.database": "system",
    "app.main": "system",
    "app.tasks": "task",
    "app.routers": "web",
}

SEVERITY_LABELS = {
    logging.DEBUG: "debug",
    logging.INFO: "info",
    logging.WARNING: "warning",
    logging.ERROR: "error",
    logging.CRITICAL: "critical",
}

ALL_CATEGORIES = sorted({
    "fetch", "match", "generate", "enrich", "agent",
    "provider", "outreach", "system", "task", "web", "other",
})


@dataclass
class ActivityEvent:
    timestamp: float
    when: str
    severity: str
    level: int
    category: str
    logger_name: str
    message: str
    exc_text: str | None = None

    @property
    def is_error(self) -> bool:
        return self.level >= logging.ERROR

    @property
    def is_warning(self) -> bool:
        return self.level >= logging.WARNING

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "when": self.when,
            "severity": self.severity,
            "level": self.level,
            "category": self.category,
            "logger_name": self.logger_name,
            "message": self.message,
            "exc_text": self.exc_text,
            "is_error": self.is_error,
            "is_warning": self.is_warning,
        }


class ActivityBuffer:
    """Thread-safe ring buffer holding the last MAX_EVENTS log records."""

    def __init__(self, maxlen: int = MAX_EVENTS):
        self._buf: deque[ActivityEvent] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._seq = 0

    def append(self, event: ActivityEvent) -> None:
        with self._lock:
            self._seq += 1
            self._buf.append(event)

    def recent(
        self,
        limit: int = 50,
        severity: str | None = None,
        category: str | None = None,
        since: float | None = None,
    ) -> list[ActivityEvent]:
        with self._lock:
            events = list(self._buf)
        events.reverse()
        if since is not None:
            events = [e for e in events if e.timestamp > since]
        if severity:
            min_level = _severity_to_level(severity)
            events = [e for e in events if e.level >= min_level]
        if category:
            events = [e for e in events if e.category == category]
        return events[:limit]

    def counts(self) -> dict:
        with self._lock:
            events = list(self._buf)
        now = time.time()
        hour_ago = now - 3600
        recent = [e for e in events if e.timestamp > hour_ago]
        return {
            "total": len(events),
            "last_hour": len(recent),
            "errors": sum(1 for e in recent if e.is_error),
            "warnings": sum(1 for e in recent if e.is_warning and not e.is_error),
        }

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()


def _severity_to_level(severity: str) -> int:
    for level, label in SEVERITY_LABELS.items():
        if label == severity:
            return level
    return logging.WARNING


def _categorize(name: str) -> str:
    for prefix, cat in CATEGORY_MAP.items():
        if name == prefix or name.startswith(prefix + "."):
            return cat
    if name.startswith("app.tasks"):
        return "task"
    if name.startswith("app.routers"):
        return "web"
    if name.startswith("app."):
        return "system"
    return "other"


_buffer = ActivityBuffer()


class ActivityLogHandler(logging.Handler):
    """
    Logging handler that feeds interesting records into the activity buffer.

    Attached to the root ``app`` logger so it sees everything the application
    emits. Only WARNING+ is captured by default; INFO is captured for a few
    loggers where the message is user-relevant (fetch completions, task
    starts).
    """

    INFO_LOGGERS = frozenset({
        "app.main",
        "app.services.job_fetcher",
        "app.services.matcher",
        "app.services.doc_generator",
        "app.services.enrichment",
        "app.services.provider_check",
        "app.services.backups",
        "app.services.browser_tasks",
        "app.services.browse_plan",
        "app.services.outreach_sender",
        "app.services.mailbox",
        "app.tasks.fetch",
        "app.tasks.match",
        "app.tasks.generate",
        "app.tasks.enrich",
        "app.tasks.backup",
    })

    def __init__(self, buffer: ActivityBuffer | None = None):
        super().__init__()
        self._buffer = buffer or _buffer

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.WARNING:
            if record.levelno < logging.INFO:
                return
            if record.name not in self.INFO_LOGGERS:
                return

        try:
            dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
            event = ActivityEvent(
                timestamp=record.created,
                when=dt.strftime("%Y-%m-%d %H:%M:%S"),
                severity=SEVERITY_LABELS.get(record.levelno, "info"),
                level=record.levelno,
                category=_categorize(record.name),
                logger_name=record.name,
                message=self.format(record) if not record.getMessage() else record.getMessage(),
                exc_text=record.exc_text if record.exc_text else None,
            )
            self._buffer.append(event)
        except Exception:
            pass


def install_handler() -> ActivityLogHandler:
    handler = ActivityLogHandler(_buffer)
    handler.setLevel(logging.DEBUG)
    app_logger = logging.getLogger("app")
    app_logger.addHandler(handler)
    return handler


def recent(
    limit: int = 50,
    severity: str | None = None,
    category: str | None = None,
    since: float | None = None,
) -> list[ActivityEvent]:
    return _buffer.recent(limit=limit, severity=severity, category=category, since=since)


def counts() -> dict:
    return _buffer.counts()


def clear() -> None:
    _buffer.clear()


def categories() -> list[str]:
    return ALL_CATEGORIES
