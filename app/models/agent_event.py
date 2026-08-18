import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


# What an event can be. A closed set, because the whole point of this table is
# to be groupable — a free-text kind produces "harvest", "harvested",
# "harvest_linkedin" and no usable count of any of them. Unknown kinds arriving
# from a client are filed under `other` rather than rejected, so an extension
# newer than the server still leaves a trace.
KINDS = (
    "poll",             # an agent asked for work
    "task_done",        # a browser task came back
    "task_failed",
    "harvest",          # job JSON the browser saw, offered unasked
    "overlay_open",     # the panel was opened on a posting
    "autofill",         # Fill this form
    "attach_resume",
    "mark_applied",
    "prepare",          # "save and write documents" from the overlay
    "error",            # the extension caught something worth reporting
    "other",
)


class AgentEvent(Base):
    """
    What the browser extension actually did, kept where it can be counted.

    Everything the extension does happens on someone else's page and leaves no
    trace here. A harvest that found nothing, an autofill that recognised two
    fields out of fifteen, an overlay lookup on a site the URL matcher cannot
    resolve — all of them are silence, and silence is indistinguishable from
    the extension not running at all. That ambiguity is the single most
    persistent problem with this subsystem: every question about it starts with
    "is it even installed?"

    So one row per event, deliberately shaped for aggregation rather than for
    reading individually:

    * **`kind` is a closed set** (see `KINDS`) because the questions are all
      counts — how many harvests this week, what fraction of autofills matched
      anything, which kinds of task fail.
    * **`host`, never the URL.** The URL is the posting, which is already in
      `jobs`; the host is what you group by when asking which sites the
      extension is failing on. It also means this table is not a browsing
      history.
    * **`summary` is JSONB** and free-shaped, because what is worth recording
      about an autofill (fields matched, fields skipped) has nothing in common
      with what is worth recording about a harvest (jobs found, jobs new).

    Pruned on a timer like the LLM log. A diagnostic that fills the disk stops
    being a diagnostic.
    """

    __tablename__ = "agent_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now(),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False, default="other")
    # The site it happened on, not the page. See the class docstring.
    host: Mapped[str | None] = mapped_column(String(160), nullable=True)
    # Which browser reported it. Several can run at once — a laptop and a
    # desktop — and "the extension is broken" is usually only true of one.
    agent_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        Index("ix_agent_events_created", created_at.desc()),
        Index("ix_agent_events_kind_created", "kind", created_at.desc()),
    )
