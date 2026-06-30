"""Phase 4 randomization: assignment modes, shuffle maps, topic scores

Revision ID: 006
Revises: 005
Create Date: 2026-06-30
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── assignments: mode + shuffle toggles + topic rules ─────────────────────
    op.add_column("assignments", sa.Column(
        "mode", sa.String(20), nullable=False, server_default="manual"
    ))
    op.add_column("assignments", sa.Column(
        "randomize_questions", sa.Boolean, nullable=False, server_default="false"
    ))
    op.add_column("assignments", sa.Column(
        "randomize_options", sa.Boolean, nullable=False, server_default="false"
    ))
    # topic_rules: [{topic, count}] — used by 'randomized' and 'total_random' modes
    op.add_column("assignments", sa.Column(
        "topic_rules", JSONB, nullable=True
    ))

    # ── results: shuffle maps per student ────────────────────────────────────
    # question_order: list of question IDs in the order shown to this student
    op.add_column("results", sa.Column(
        "question_order", JSONB, nullable=True
    ))
    # option_orders: {question_id: [shuffled indices]} — maps display position → original index
    op.add_column("results", sa.Column(
        "option_orders", JSONB, nullable=True
    ))

    # ── result_topic_scores ───────────────────────────────────────────────────
    op.create_table(
        "result_topic_scores",
        sa.Column("id",        UUID(as_uuid=False), primary_key=True),
        sa.Column("result_id", UUID(as_uuid=False),
                  sa.ForeignKey("results.id", ondelete="CASCADE"), nullable=False),
        sa.Column("topic",   sa.String(255), nullable=False),
        sa.Column("correct", sa.Integer,     nullable=False),
        sa.Column("total",   sa.Integer,     nullable=False),
    )
    op.create_index("ix_result_topic_scores_result_id", "result_topic_scores", ["result_id"])


def downgrade() -> None:
    op.drop_index("ix_result_topic_scores_result_id", table_name="result_topic_scores")
    op.drop_table("result_topic_scores")
    op.drop_column("results",     "option_orders")
    op.drop_column("results",     "question_order")
    op.drop_column("assignments", "topic_rules")
    op.drop_column("assignments", "randomize_options")
    op.drop_column("assignments", "randomize_questions")
    op.drop_column("assignments", "mode")
