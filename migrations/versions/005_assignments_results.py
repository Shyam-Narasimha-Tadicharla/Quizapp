"""assignments, results, answers; soft-delete on questions

Revision ID: 005
Revises: 004
Create Date: 2026-06-30
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Soft delete on questions ──────────────────────────────────────────────
    op.add_column("questions", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))

    # ── assignments ───────────────────────────────────────────────────────────
    # A teacher assigns a quiz to a class by setting a class_name code.
    # Students enter this code on the /take page to access the quiz.
    op.create_table(
        "assignments",
        sa.Column("id",         UUID(as_uuid=False), primary_key=True),
        sa.Column("school_id",  UUID(as_uuid=False), sa.ForeignKey("schools.id"), nullable=False),
        sa.Column("quiz_id",    UUID(as_uuid=False), sa.ForeignKey("quizzes.id",  ondelete="CASCADE"), nullable=False),
        sa.Column("created_by", UUID(as_uuid=False), sa.ForeignKey("users.id"),   nullable=False),
        sa.Column("class_name", sa.String(100), nullable=False),
        sa.Column("opens_at",   sa.DateTime(timezone=True), nullable=True),
        sa.Column("closes_at",  sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_assignments_school_id",  "assignments", ["school_id"])
    op.create_index("ix_assignments_class_name", "assignments", ["class_name"])

    # ── results ───────────────────────────────────────────────────────────────
    op.create_table(
        "results",
        sa.Column("id",            UUID(as_uuid=False), primary_key=True),
        sa.Column("assignment_id", UUID(as_uuid=False), sa.ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("student_name",  sa.String(255), nullable=False),
        sa.Column("roll_number",   sa.String(50),  nullable=False),
        sa.Column("class_name",    sa.String(100), nullable=False),
        sa.Column("score",         sa.Integer, nullable=False),
        sa.Column("total",         sa.Integer, nullable=False),
        sa.Column("submitted_at",  sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_results_assignment_id", "results", ["assignment_id"])

    # ── answers ───────────────────────────────────────────────────────────────
    op.create_table(
        "answers",
        sa.Column("id",            UUID(as_uuid=False), primary_key=True),
        sa.Column("result_id",     UUID(as_uuid=False), sa.ForeignKey("results.id",   ondelete="CASCADE"), nullable=False),
        sa.Column("question_id",   UUID(as_uuid=False), sa.ForeignKey("questions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("chosen_index",  sa.Integer, nullable=False),
        sa.Column("is_correct",    sa.Boolean, nullable=False),
    )
    op.create_index("ix_answers_result_id", "answers", ["result_id"])


def downgrade() -> None:
    op.drop_index("ix_answers_result_id",       table_name="answers")
    op.drop_table("answers")
    op.drop_index("ix_results_assignment_id",   table_name="results")
    op.drop_table("results")
    op.drop_index("ix_assignments_class_name",  table_name="assignments")
    op.drop_index("ix_assignments_school_id",   table_name="assignments")
    op.drop_table("assignments")
    op.drop_column("questions", "deleted_at")
