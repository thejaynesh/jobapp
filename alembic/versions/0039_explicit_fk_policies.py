"""Say what happens to the children, and index the join.

Three schema gaps, all of them latent: nothing in the application deletes a Job
or an Application except `services.archive`, and that pre-filters rows that
have applications. They are the kind that stay latent right up until the filter
changes.

**`applications.job_id` and `application_documents.application_id` had no
`ondelete`.** Every other foreign key here declares one — `job_scores`,
`fetch_source_runs`, `contacts`, `outreach_messages` — so these two were the
exception rather than a decision. Without a policy the database defaults to NO
ACTION, which means `archive`'s bulk `DELETE FROM jobs WHERE id IN (...)` would
raise on a constraint and roll back the whole 5,000-row batch rather than
failing one row.

**`contacts.application_id` was CASCADE, and its own comment says the
opposite.** "Nullable so a contact can outlive the application" — but CASCADE
deletes the contact with the application. Nullable is not SET NULL, and a
contact is a person at a company who keeps being one after an application goes
away. `outreach_messages.application_id` stays CASCADE on purpose: a message is
*about* an application in a way a person is not.

**`applications.job_id` was not indexed.** Postgres indexes the referenced
primary key, not the referencing column, so every join from applications to
jobs — `archive._eligible`, `generate.sweep_generations`,
`deduplication.find_duplicate_application_job`, the funnel — had no index to
use. The applications table is small for one person's search, so this is
cheap insurance rather than a measured win, which is why it rides along here.

Revision ID: 0039
Revises: 0038
"""
from typing import Union

from alembic import op

revision: str = "0039"
down_revision: Union[str, None] = "0038"
branch_labels = None
depends_on = None

# (constraint, table, referenced table, local column, remote column, ondelete)
_KEYS = [
    ("applications_job_id_fkey", "applications", "jobs",
     "job_id", "id", "CASCADE"),
    ("application_documents_application_id_fkey", "application_documents",
     "applications", "application_id", "id", "CASCADE"),
    ("contacts_application_id_fkey", "contacts", "applications",
     "application_id", "id", "SET NULL"),
]


def upgrade() -> None:
    for name, table, referent, local, remote, ondelete in _KEYS:
        # Named explicitly rather than reflected: Postgres' own default naming
        # is what these were created with, and guessing it here is more
        # readable than a reflection dance.
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(
            name, table, referent, [local], [remote], ondelete=ondelete
        )
    op.create_index("ix_applications_job_id", "applications", ["job_id"])
    op.create_index(
        "ix_application_documents_application_id",
        "application_documents", ["application_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_application_documents_application_id",
                  table_name="application_documents")
    op.drop_index("ix_applications_job_id", table_name="applications")
    for name, table, referent, local, remote, _ in _KEYS:
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(name, table, referent, [local], [remote])
