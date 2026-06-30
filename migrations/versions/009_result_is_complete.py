"""Add is_complete to results to distinguish submitted vs abandoned attempts

Revision ID: 009
Revises: 008
Create Date: 2026-06-30
"""

from alembic import op
import sqlalchemy as sa

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "results",
        sa.Column("is_complete", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("results", "is_complete")
