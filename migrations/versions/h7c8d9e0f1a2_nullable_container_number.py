"""make container_number nullable for BL-only shipments

Revision ID: h7c8d9e0f1a2
Revises: g6b7c8d9e0f1
Create Date: 2026-03-19 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'h7c8d9e0f1a2'
down_revision: Union[str, Sequence[str], None] = 'g6b7c8d9e0f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('shipments', 'container_number', nullable=True)


def downgrade() -> None:
    # Fill nulls before reverting to NOT NULL
    op.execute("UPDATE shipments SET container_number = 'UNKNOWN' WHERE container_number IS NULL")
    op.alter_column('shipments', 'container_number', nullable=False)
