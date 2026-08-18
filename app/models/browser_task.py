"""
Work the server wants a browser to do.

The VPS can reach an API but it cannot reach LinkedIn as *you* — no residential
IP, no logged-in session, no real fingerprint. So anything needing those becomes
a row here, and whichever engine is awake on the laptop picks it up. The server
never calls the laptop; the laptop asks for work. That inversion is the whole
point: a laptop that is closed, asleep, or on a plane is not an error condition,
it is Tuesday.

Consequences the schema has to carry:

  - **The fetch cycle must never block on this.** Enqueue and move on. A task
    with no agent to run it simply expires, and the pipeline is no worse off
    than before it was queued.
  - **A leased task is not a finished one.** An agent that takes work and then
    closes its lid would otherwise strand that row forever, so a lease carries
    its own deadline and returns to the queue when it lapses.
  - **Work goes stale.** Resolving a job link is worth doing today and pointless
    next week, so tasks expire on their own rather than accumulating into a
    backlog nobody wants executed.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# What an agent can be asked to do. Plain strings rather than a PG enum: this
# vocabulary grows every time a new site needs handling, and a new capability
# should not need a type migration to try out.
#
#   ping          — echo the payload back. Proves the round trip end to end
#                   without depending on any site being up. Never expires into
#                   anything meaningful; it exists to be diagnostic.
#   fetch_page    — retrieve a URL using the user's own session and return what
#                   came back.
#   fetch_json    — retrieve a JSON endpoint the server is blocked from. Reddit
#                   answers a datacenter IP with 403 and a browser with data;
#                   the difference is the residential IP, which is the whole
#                   reason this queue exists. `purpose` in the payload says what
#                   the result is for, so one kind serves every blocked source.
#   resolve_link  — follow an aggregator redirect to the real apply URL.
#   harvest_jobs  — hand back job JSON the content script intercepted.
#
# `harvest_jobs` is named but unused: harvesting is a push from the content
# script rather than work anyone queues, and the name is kept so a future
# pull-based variant has somewhere to live.
TASK_KINDS = ("ping", "fetch_page", "fetch_json", "resolve_link", "harvest_jobs")

# queued  — waiting for an agent
# leased  — an agent holds it, with a deadline
# done    — completed, `result` populated
# failed  — gave up after max_attempts, `error` populated
# expired — nobody ran it in time
TASK_STATUSES = ("queued", "leased", "done", "failed", "expired")

# Statuses that will never change again.
TERMINAL_TASK_STATUSES = ("done", "failed", "expired")


class BrowserTask(Base):
    """One unit of work for whichever browser engine gets to it first."""

    __tablename__ = "browser_tasks"
    __table_args__ = (
        # The leasing query: queued tasks of some kind, oldest and highest
        # priority first. This is the only hot path on the table.
        Index("ix_browser_tasks_claimable", "status", "kind", "priority", "created_at"),
        # The reaper scans both deadlines.
        Index("ix_browser_tasks_lease_expires_at", "lease_expires_at"),
        Index("ix_browser_tasks_expires_at", "expires_at"),
        # Finished tasks, oldest first — what the pruner reads. Nothing pruned
        # this table until there was somewhere else to keep the history, so
        # this index had no reason to exist before now.
        Index("ix_browser_tasks_completed_at", "completed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    kind: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String, nullable=False, default="queued")

    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Higher runs first. Same-priority work is FIFO, so a burst of link
    # resolutions cannot starve one urgent task queued behind it.
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # A failing task is retried, but not forever: a link that 404s will 404
    # again, and retrying it every poll is just noise in the queue.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)

    # Which agent holds it. Free-form and self-reported — it identifies engines
    # for debugging, and is not a credential. The bearer token is the only thing
    # that decides whether a caller may lease at all.
    agent_id: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    leased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # When the current lease lapses and the task goes back in the queue.
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # When the task stops being worth doing at all.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_TASK_STATUSES

    def as_dict(self) -> dict:
        """The shape handed to an agent. Deliberately not the whole row."""
        return {
            "id": str(self.id),
            "kind": self.kind,
            "payload": self.payload or {},
            "attempts": self.attempts,
            "lease_expires_at": (
                self.lease_expires_at.isoformat() if self.lease_expires_at else None
            ),
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<BrowserTask {self.kind} {self.status} {self.id}>"
