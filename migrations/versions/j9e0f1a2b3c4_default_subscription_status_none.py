"""change default subscription_status from pending to none

Revision ID: j9e0f1a2b3c4
Revises: i8d9e0f1a2b3
Create Date: 2026-03-21 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "j9e0f1a2b3c4"
down_revision: Union[str, None] = "i8d9e0f1a2b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "users",
        "subscription_status",
        server_default="none",
    )
    # Update existing rows that still have the old default
    op.execute("UPDATE users SET subscription_status = 'none' WHERE subscription_status = 'pending'")


def downgrade() -> None:
    op.alter_column(
        "users",
        "subscription_status",
        server_default="pending",
    )
    op.execute("UPDATE users SET subscription_status = 'pending' WHERE subscription_status = 'none'")
