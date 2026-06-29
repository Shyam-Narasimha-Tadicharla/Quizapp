"""initial schema

Revision ID: 001
Revises:
Create Date: 2026-06-29
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # schools — added now so school_id FK exists before Phase 2 needs it.
    # Phase 1 never inserts into this table; it's a placeholder.
    op.create_table(
        "schools",
        sa.Column("id",         postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("name",       sa.String(255),                 nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),     nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "quizzes",
        sa.Column("id",              postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("school_id",       postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("title",           sa.String(500),                 nullable=False),
        sa.Column("source_filename", sa.String(500),                 nullable=False,
                  server_default=""),
        sa.Column("created_at",      sa.DateTime(timezone=True),     nullable=False),
        sa.ForeignKeyConstraint(["school_id"], ["schools.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    # Index speeds up the ORDER BY created_at DESC in list_quizzes()
    op.create_index("ix_quizzes_created_at", "quizzes", ["created_at"])

    op.create_table(
        "questions",
        sa.Column("id",            postgresql.UUID(as_uuid=False),  nullable=False),
        sa.Column("school_id",     postgresql.UUID(as_uuid=False),  nullable=True),
        sa.Column("text",          sa.Text(),                       nullable=False),
        sa.Column("options",       postgresql.ARRAY(sa.Text()),     nullable=False),
        sa.Column("correct_index", sa.Integer(),                    nullable=False),
        sa.ForeignKeyConstraint(["school_id"], ["schools.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "quiz_questions",
        sa.Column("quiz_id",     postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("question_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("position",    sa.Integer(),                   nullable=False),
        sa.ForeignKeyConstraint(["question_id"], ["questions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["quiz_id"],     ["quizzes.id"],   ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("quiz_id", "question_id"),
        sa.UniqueConstraint("quiz_id", "position", name="uq_quiz_question_position"),
    )


def downgrade() -> None:
    op.drop_table("quiz_questions")
    op.drop_index("ix_quizzes_created_at", table_name="quizzes")
    op.drop_table("questions")
    op.drop_table("quizzes")
    op.drop_table("schools")
