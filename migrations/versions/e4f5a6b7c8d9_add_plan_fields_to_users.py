"""add plan fields to users

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-03-18 16:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e4f5a6b7c8d9"
down_revision: Union[str, Sequence[str], None] = "d3e4f5a6b7c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("plan", sa.String(32), nullable=True))
    op.add_column("users", sa.Column("payment_pending_plan", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "payment_pending_plan")
    op.drop_column("users", "plan")
