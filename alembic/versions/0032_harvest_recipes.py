"""Keep the payloads a site sent us, and what we learned to do with them.

The harvest reader is shape-based: it walks a payload looking for anything with
a title, a company and an identifier. That works on a site nobody wrote a
parser for, which is most of them — and it has two failure modes it cannot
handle on its own.

A payload can name its fields something no alias knows, and nothing comes out.
Or — worse, because it looks like success — it can be *normalized*, with the
job holding a reference to a company that lives elsewhere in the response. The
walker then matches `companyUrn`, and stores `urn:li:fsd_company:1234` as an
employer name. Joining across a payload is exactly what a walker cannot do.

Both are fixable if you can see the payload, and until now the payload was
thrown away the moment extraction returned nothing. So:

`harvest_samples` keeps a bounded, truncated copy of what a host sent when we
made nothing of it. It is evidence, not storage: capped per host, expired on a
timer, and truncated hard, because these are responses to a logged-in session
and can carry names and account identifiers.

`harvest_recipes` keeps what was learned from those samples — a declarative
description of where the jobs live and which fields to read, interpreted by our
own code. Deliberately not generated code: a recipe can be read, diffed,
validated against a sample and rejected before it goes near the pipeline, none
of which is true of a parser nobody wrote.

Revision ID: 0032
Revises: 0031
"""
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0032"
down_revision: Union[str, None] = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "harvest_samples",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("host", sa.String(length=160), nullable=False),
        sa.Column("source_url", sa.String(length=1000), nullable=True),
        # The payload as received, truncated. JSONB rather than text so a
        # recipe can be tried against it without re-parsing.
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("bytes", sa.Integer(), nullable=False, server_default="0"),
        # What the shape-based walker made of it. Zero is the case this exists
        # for; a non-zero sample is kept when the yield looked wrong rather
        # than absent.
        sa.Column("found", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    # The only query: this host's samples, newest first, for proposing a recipe
    # and for pruning.
    op.create_index(
        "ix_harvest_samples_host_created", "harvest_samples",
        ["host", "created_at"],
    )

    op.create_table(
        "harvest_recipes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("host", sa.String(length=160), nullable=False),
        sa.Column("recipe", postgresql.JSONB(), nullable=False),
        # proposed — written, not yet trusted
        # active   — validated against samples and in use
        # rejected — failed validation, or replaced by a better one
        sa.Column("status", sa.String(length=20), nullable=False,
                  server_default="proposed"),
        # What validation actually saw, kept so a recipe that degrades can be
        # compared against the day it was accepted.
        sa.Column("jobs_found", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("samples_tried", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("model", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_harvest_recipes_host", "harvest_recipes", ["host"])
    # At most one active recipe per host. Two would make extraction depend on
    # row order, which is the kind of bug that looks like the site changing.
    op.create_index(
        "uq_harvest_recipes_active", "harvest_recipes", ["host"],
        unique=True, postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("uq_harvest_recipes_active", table_name="harvest_recipes")
    op.drop_index("ix_harvest_recipes_host", table_name="harvest_recipes")
    op.drop_table("harvest_recipes")
    op.drop_index("ix_harvest_samples_host_created", table_name="harvest_samples")
    op.drop_table("harvest_samples")
