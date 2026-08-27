"""Learn how to walk a board, the way we already learn how to read one.

`harvest_recipes` answers "where are the jobs in this payload". This answers the
question before it: "how do I get the page to show me more of them".

Boards reach their second page three different ways. Some put it in the URL
(`start=25`). Some have no second page at all and load as you scroll. Some have
numbered buttons and serve every page from one address, so the only way through
is to click. Using the wrong one harvests page one forever while every number on
the panel looks healthy — the visit succeeds, the scroll reports a sensible
depth, rows arrive, and they are the same rows every time.

Until now each board's mechanism was hand-written into `browse_plan.BOARDS`,
which means a board that changes its pagination, or one nobody has classified
yet, quietly yields a fraction of itself and says nothing. That does not scale
past the boards somebody happened to notice.

So the same two-table shape as the harvest recipes, for the same reasons:

`crawl_samples` is evidence. When a visit fails to get past the first screenful,
the extension sends back a trimmed description of what the page offered — the
candidate controls, whether scrolling grew anything, the URL's own parameters.
Not the page: a bounded list of things that might be a "next" button.

`crawl_recipes` is the conclusion — mode, and whatever that mode needs.
Declarative and interpreted by our own code rather than generated, because a
click on a logged-in job board is not a thing to run a model's code for. A
recipe can be read, checked against the sample it came from, refused for
pointing at something destructive, and retired automatically when the visits it
produces still only reach page one.

Revision ID: 0033
Revises: 0032
"""
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0033"
down_revision: Union[str, None] = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "crawl_samples",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("host", sa.String(length=160), nullable=False),
        sa.Column("source_url", sa.String(length=1000), nullable=True),
        # What the page offered: candidate controls, scroll behaviour, the
        # URL's existing query parameters. Bounded by the extension before it
        # is sent — this is a description of the navigation, not the document.
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        # How far the visit that produced this got. The reason it was kept.
        sa.Column("pages_reached", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("batches", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("note", sa.String(length=200), nullable=True),
        # clock_timestamp() rather than now(): now() is the *transaction*
        # start time, so every row written in one transaction shares a
        # timestamp and "newest first" becomes arbitrary. These are event
        # records, and two written in one transaction did happen in an order.
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
    )
    op.create_index(
        "ix_crawl_samples_host_created", "crawl_samples", ["host", "created_at"],
    )

    op.create_table(
        "crawl_recipes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("host", sa.String(length=160), nullable=False),
        # {"mode": "click"|"scroll"|"url", ...}. See services.crawl_recipes.
        sa.Column("recipe", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False,
                  server_default="proposed"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("model", sa.String(length=120), nullable=True),
        # How the visits made under this recipe have actually gone. A recipe
        # that validates against a sample and then never gets past page one in
        # the wild is wrong, and this is what notices — the sample cannot,
        # because it is a snapshot of the page rather than of the outcome.
        sa.Column("tries", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("best_pages", sa.Integer(), nullable=False, server_default="0"),
        # clock_timestamp() rather than now(): now() is the *transaction*
        # start time, so every row written in one transaction shares a
        # timestamp and "newest first" becomes arbitrary. These are event
        # records, and two written in one transaction did happen in an order.
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_crawl_recipes_host", "crawl_recipes", ["host"])
    # At most one active recipe per host, for the same reason the harvest
    # recipes have this: two would make a crawl depend on row order.
    op.create_index(
        "uq_crawl_recipes_active", "crawl_recipes", ["host"],
        unique=True, postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("uq_crawl_recipes_active", table_name="crawl_recipes")
    op.drop_index("ix_crawl_recipes_host", table_name="crawl_recipes")
    op.drop_table("crawl_recipes")
    op.drop_index("ix_crawl_samples_host_created", table_name="crawl_samples")
    op.drop_table("crawl_samples")
