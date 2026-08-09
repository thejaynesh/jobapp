"""Outreach contacts and message sequences.

Contacts and their messages move out of applications.outreach_contacts (a JSONB
blob with no status, no history, and no way to schedule anything) into two real
tables. The old column is kept and backfilled from, not dropped, so nothing
already discovered is lost.

Revision ID: 0012
Revises: 0011
"""
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "applications",
        sa.Column("outreach_status", sa.String(20), nullable=False, server_default="idle"),
    )
    op.add_column("applications", sa.Column("outreach_error", sa.Text(), nullable=True))
    op.add_column(
        "applications",
        sa.Column("outreach_checked_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "contacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("application_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("company", sa.String(), nullable=False),
        sa.Column("company_key", sa.String(), nullable=False),
        sa.Column("domain", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("first_name", sa.String(), nullable=True),
        sa.Column("last_name", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("department", sa.String(), nullable=True),
        sa.Column("role", sa.String(), nullable=False, server_default="unknown"),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("email_status", sa.String(), nullable=False, server_default="unknown"),
        sa.Column("email_confidence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "alternate_emails", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"
        ),
        sa.Column("linkedin_url", sa.String(), nullable=True),
        sa.Column("twitter", sa.String(), nullable=True),
        sa.Column("phone", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=False, server_default="manual"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"], ondelete="CASCADE"),
        # NULL emails compare as distinct, so several unnamed contacts on one
        # application coexist while a duplicate address cannot.
        sa.UniqueConstraint("application_id", "email", name="uq_contacts_application_email"),
    )
    op.create_index("ix_contacts_company_key", "contacts", ["company_key"])
    op.create_index("ix_contacts_application_id", "contacts", ["application_id"])

    op.create_table(
        "outreach_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("application_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("channel", sa.String(), nullable=False, server_default="email"),
        sa.Column("kind", sa.String(), nullable=False, server_default="initial"),
        sa.Column("sequence_step", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("tone", sa.String(), nullable=False, server_default="warm"),
        sa.Column("subject", sa.String(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(), nullable=False, server_default="draft"),
        sa.Column("feedback", sa.Text(), nullable=True),
        sa.Column("generated_by", sa.String(), nullable=True),
        sa.Column("edited", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("follow_up_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("send_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_outreach_messages_contact_id", "outreach_messages", ["contact_id"])
    op.create_index("ix_outreach_messages_application_id", "outreach_messages", ["application_id"])
    op.create_index("ix_outreach_messages_status", "outreach_messages", ["status"])
    op.create_index(
        "ix_outreach_messages_follow_up_due_at", "outreach_messages", ["follow_up_due_at"]
    )

    _backfill_from_jsonb()


def _backfill_from_jsonb() -> None:
    """
    Move whatever the old JSONB column holds into the new tables.

    Records there have a name, a title, an email, and a message and nothing
    else, so everything they become is a draft on an unverified address. The
    ON CONFLICT guard covers the old code appending the same person twice to
    one application, which it did on every re-run.
    """
    import json
    import uuid

    from app.services.company_domain import company_key

    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT a.id, j.company, a.outreach_contacts "
            "FROM applications a JOIN jobs j ON j.id = a.job_id "
            "WHERE a.outreach_contacts IS NOT NULL "
            "AND jsonb_array_length(a.outreach_contacts) > 0"
        )
    ).fetchall()

    for application_id, company, payload in rows:
        entries = json.loads(payload) if isinstance(payload, str) else (payload or [])
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            email = (entry.get("email") or "").strip().lower() or None
            name = (entry.get("name") or "").strip() or None
            if not (email or name):
                continue
            contact_id = uuid.uuid4()
            inserted = connection.execute(
                sa.text(
                    "INSERT INTO contacts (id, application_id, company, company_key, name, "
                    "title, email, email_status, email_confidence, source, role) "
                    "VALUES (:id, :application_id, :company, :company_key, :name, :title, "
                    ":email, :email_status, :confidence, 'hunter', 'unknown') "
                    "ON CONFLICT (application_id, email) DO NOTHING RETURNING id"
                ),
                {
                    "id": contact_id,
                    "application_id": application_id,
                    "company": company or "",
                    "company_key": company_key(company or ""),
                    "name": name,
                    "title": (entry.get("title") or "").strip() or None,
                    "email": email,
                    "email_status": "unverified" if email else "unknown",
                    "confidence": 50 if email else 0,
                },
            ).fetchone()
            if inserted is None:
                continue
            message = (entry.get("message") or "").strip()
            if not message:
                continue
            connection.execute(
                sa.text(
                    "INSERT INTO outreach_messages (id, contact_id, application_id, channel, "
                    "kind, sequence_step, tone, body, status) "
                    "VALUES (:id, :contact_id, :application_id, 'linkedin', 'initial', 1, "
                    "'warm', :body, 'draft')"
                ),
                {
                    "id": uuid.uuid4(),
                    "contact_id": contact_id,
                    "application_id": application_id,
                    "body": message,
                },
            )


def downgrade() -> None:
    op.drop_index("ix_outreach_messages_follow_up_due_at", table_name="outreach_messages")
    op.drop_index("ix_outreach_messages_status", table_name="outreach_messages")
    op.drop_index("ix_outreach_messages_application_id", table_name="outreach_messages")
    op.drop_index("ix_outreach_messages_contact_id", table_name="outreach_messages")
    op.drop_table("outreach_messages")
    op.drop_index("ix_contacts_application_id", table_name="contacts")
    op.drop_index("ix_contacts_company_key", table_name="contacts")
    op.drop_table("contacts")
    op.drop_column("applications", "outreach_checked_at")
    op.drop_column("applications", "outreach_error")
    op.drop_column("applications", "outreach_status")
