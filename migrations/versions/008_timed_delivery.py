"""Add duration_minutes to assignments and started_at to results

Revision ID: 008
Revises: 007
Create Date: 2026-06-30
"""

from alembic import op
import sqlalchemy as sa

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("assignments", sa.Column("duration_minutes", sa.Integer(), nullable=True))
    op.add_column("results",     sa.Column("started_at",       sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("assignments", "duration_minutes")
    op.drop_column("results",     "started_at")
