"""subjects, subject_topics, user_subjects — Phase 4

Revision ID: 004
Revises: 003
Create Date: 2026-06-30
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add email to users table for display purposes
    op.add_column("users", sa.Column("email", sa.String(255), nullable=True))

    # subjects: one per school, named grouping (e.g. "Mathematics", "Sciences")
    op.create_table(
        "subjects",
        sa.Column("id",         UUID(as_uuid=False), primary_key=True),
        sa.Column("school_id",  UUID(as_uuid=False), sa.ForeignKey("schools.id"), nullable=False),
        sa.Column("name",       sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_subjects_school_id", "subjects", ["school_id"])
    op.create_unique_constraint("uq_subject_school_name", "subjects", ["school_id", "name"])

    # subject_topics: maps a free-form topic string to a subject
    op.create_table(
        "subject_topics",
        sa.Column("subject_id", UUID(as_uuid=False), sa.ForeignKey("subjects.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("topic",      sa.String(255), primary_key=True),
    )

    # user_subjects: assigns one or more subjects to a teacher (or admin)
    op.create_table(
        "user_subjects",
        sa.Column("user_id",    UUID(as_uuid=False), sa.ForeignKey("users.id",    ondelete="CASCADE"), primary_key=True),
        sa.Column("subject_id", UUID(as_uuid=False), sa.ForeignKey("subjects.id", ondelete="CASCADE"), primary_key=True),
    )


def downgrade() -> None:
    op.drop_table("user_subjects")
    op.drop_table("subject_topics")
    op.drop_index("ix_subjects_school_id", table_name="subjects")
    op.drop_constraint("uq_subject_school_name", "subjects", type_="unique")
    op.drop_table("subjects")
    op.drop_column("users", "email")
