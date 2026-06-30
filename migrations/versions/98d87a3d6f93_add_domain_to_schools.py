"""add_domain_to_schools

Revision ID: 98d87a3d6f93
Revises: 009
Create Date: 2026-07-01 01:08:25.636941

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '98d87a3d6f93'
down_revision: Union[str, Sequence[str], None] = '009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('schools', sa.Column('domain', sa.String(length=253), nullable=True))
    op.create_unique_constraint('uq_schools_domain', 'schools', ['domain'])


def downgrade() -> None:
    op.drop_constraint('uq_schools_domain', 'schools', type_='unique')
    op.drop_column('schools', 'domain')
