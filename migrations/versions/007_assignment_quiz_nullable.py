"""Make assignments.quiz_id nullable for total_random mode

Revision ID: 007
Revises: 006
Create Date: 2026-06-30
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("assignments", "quiz_id", nullable=True)


def downgrade() -> None:
    op.alter_column("assignments", "quiz_id", nullable=False)
