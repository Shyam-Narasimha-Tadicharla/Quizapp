"""question bank — add topic to questions

Revision ID: 003
Revises: 002
Create Date: 2026-06-30
"""

from alembic import op
import sqlalchemy as sa

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("questions", sa.Column("topic", sa.String(255), nullable=True))
    # Index so GET /api/questions?topic=X is fast
    op.create_index("ix_questions_topic", "questions", ["school_id", "topic"])


def downgrade() -> None:
    op.drop_index("ix_questions_topic", table_name="questions")
    op.drop_column("questions", "topic")
