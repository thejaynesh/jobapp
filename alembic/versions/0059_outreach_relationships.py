"""Preserve people, conversations and discovery evidence across applications.

Application contacts remain the compatibility layer. Only exact email or a
canonical LinkedIn profile identifies an existing person; names never merge
strangers. Existing outbound messages are attached to the resulting company
conversation without changing delivery state.
"""
import uuid
from urllib.parse import urlparse

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None


def upgrade():
    uid = postgresql.UUID(as_uuid=True)
    dt = sa.DateTime(timezone=True)
    op.create_table("network_people",
        sa.Column("id", uid, primary_key=True), sa.Column("name", sa.String()),
        sa.Column("primary_email", sa.String(), unique=True),
        sa.Column("linkedin_url", sa.String(), unique=True),
        sa.Column("relationship_kind", sa.String(), nullable=False, server_default="unknown"),
        sa.Column("relationship_notes", sa.Text()),
        sa.Column("do_not_contact", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", dt, nullable=False, server_default=sa.func.now()))
    op.create_table("outreach_conversations",
        sa.Column("id", uid, primary_key=True),
        sa.Column("person_id", uid, sa.ForeignKey("network_people.id", ondelete="CASCADE"), nullable=False),
        sa.Column("company_key", sa.String(), nullable=False),
        sa.Column("company", sa.String(), nullable=False),
        sa.Column("application_id", uid, sa.ForeignKey("applications.id", ondelete="SET NULL")),
        sa.Column("status", sa.String(), nullable=False, server_default="researching"),
        sa.Column("next_action", sa.Text()), sa.Column("next_action_due_at", dt),
        sa.Column("snoozed_until", dt), sa.Column("notes", sa.Text()),
        sa.Column("last_activity_at", dt, nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", dt, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("person_id", "company_key", name="uq_outreach_conversation_person_company"))
    op.create_index("ix_outreach_conversations_next_action_due_at", "outreach_conversations", ["next_action_due_at"])
    op.create_table("outreach_interactions",
        sa.Column("id", uid, primary_key=True),
        sa.Column("conversation_id", uid, sa.ForeignKey("outreach_conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("message_id", sa.String(), unique=True),
        sa.Column("occurred_at", dt, nullable=False, server_default=sa.func.now()))
    op.create_index("ix_outreach_interactions_conversation_id", "outreach_interactions", ["conversation_id"])
    op.create_table("contact_discovery_cache",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("company_key", sa.String(), nullable=False), sa.Column("source", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("error", sa.Text()),
        sa.Column("checked_at", dt, nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", dt, nullable=False))
    op.create_index("ix_contact_discovery_cache_company_key", "contact_discovery_cache", ["company_key"])
    op.add_column("contacts", sa.Column("person_id", uid, sa.ForeignKey("network_people.id", ondelete="SET NULL")))
    op.add_column("contacts", sa.Column("evidence", postgresql.JSONB(), nullable=False, server_default="{}"))
    op.create_index("ix_contacts_person_id", "contacts", ["person_id"])
    op.add_column("outreach_messages", sa.Column("conversation_id", uid, sa.ForeignKey("outreach_conversations.id", ondelete="SET NULL")))
    op.add_column("outreach_messages", sa.Column("followup_attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("outreach_messages", sa.Column("followup_error", sa.Text()))
    op.create_index("ix_outreach_messages_conversation_id", "outreach_messages", ["conversation_id"])
    op.drop_constraint("outreach_messages_application_id_fkey", "outreach_messages", type_="foreignkey")
    op.create_foreign_key("outreach_messages_application_id_fkey", "outreach_messages", "applications", ["application_id"], ["id"], ondelete="SET NULL")
    _backfill()


def _backfill():
    bind = op.get_bind()
    emails, profiles, conversations = {}, {}, {}
    contacts = bind.execute(sa.text("SELECT id, name, email, linkedin_url, company_key, company, application_id FROM contacts ORDER BY created_at, id")).mappings()
    for contact in contacts:
        email = (contact["email"] or "").strip().lower() or None
        parsed = urlparse(contact["linkedin_url"] or "")
        linkedin = ("https://www.linkedin.com" + parsed.path.rstrip("/").lower()) if (parsed.hostname or "").lower() in {"linkedin.com", "www.linkedin.com"} and parsed.path.startswith("/in/") else None
        # Conflicting known email and profile identities stay separate.
        person_id = emails.get(email) or (profiles.get(linkedin) if not email else None)
        if person_id is None:
            person_id = uuid.uuid4()
            if linkedin in profiles:
                linkedin = None
            bind.execute(sa.text("INSERT INTO network_people(id,name,primary_email,linkedin_url) VALUES (:id,:name,:email,:linkedin)"), dict(id=person_id, name=contact["name"], email=email, linkedin=linkedin))
            if email:
                emails[email] = person_id
            if linkedin:
                profiles[linkedin] = person_id
        bind.execute(sa.text("UPDATE contacts SET person_id=:person WHERE id=:id"), dict(person=person_id, id=contact["id"]))
        company_identity = " ".join((contact["company"] or "").casefold().split())
        key = (person_id, company_identity)
        conversation = conversations.get(key)
        if conversation is None:
            conversation = uuid.uuid4()
            conversations[key] = conversation
            bind.execute(sa.text("INSERT INTO outreach_conversations(id,person_id,company_key,company,application_id) VALUES (:id,:person,:key,:company,:app)"), dict(id=conversation, person=person_id, key=company_identity, company=contact["company"], app=contact["application_id"]))
        bind.execute(sa.text("UPDATE outreach_messages SET conversation_id=:conversation WHERE contact_id=:contact"), dict(conversation=conversation, contact=contact["id"]))
    bind.execute(sa.text("""UPDATE outreach_conversations c SET status = CASE
        WHEN EXISTS (SELECT 1 FROM outreach_messages m WHERE m.conversation_id=c.id AND m.status='replied') THEN 'reply_needed'
        WHEN EXISTS (SELECT 1 FROM outreach_messages m WHERE m.conversation_id=c.id AND m.status='sent') THEN 'awaiting_reply'
        WHEN EXISTS (SELECT 1 FROM outreach_messages m WHERE m.conversation_id=c.id AND m.status IN ('draft','approved')) THEN 'ready'
        ELSE 'researching' END"""))


def downgrade():
    op.drop_constraint("outreach_messages_application_id_fkey", "outreach_messages", type_="foreignkey")
    op.create_foreign_key("outreach_messages_application_id_fkey", "outreach_messages", "applications", ["application_id"], ["id"], ondelete="CASCADE")
    for field in ("followup_error", "followup_attempts", "conversation_id"):
        op.drop_column("outreach_messages", field)
    for field in ("evidence", "person_id"):
        op.drop_column("contacts", field)
    for table in ("contact_discovery_cache", "outreach_interactions", "outreach_conversations", "network_people"):
        op.drop_table(table)
