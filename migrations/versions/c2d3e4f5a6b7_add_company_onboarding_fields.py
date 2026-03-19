"""add company onboarding fields

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-03-18 14:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c2d3e4f5a6b7"
down_revision: Union[str, Sequence[str], None] = "b1c2d3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("company_name", sa.String(255), nullable=True))
    op.add_column("users", sa.Column("tracking_email", sa.String(255), nullable=True))
    op.add_column(
        "users",
        sa.Column("subscription_status", sa.String(32), nullable=False, server_default="pending"),
    )
    op.add_column("users", sa.Column("magic_link_token", sa.String(255), nullable=True))
    op.add_column("users", sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("auth_token", sa.String(255), nullable=True))
    op.add_column("users", sa.Column("payment_session_id", sa.String(255), nullable=True))
    op.add_column("users", sa.Column("payment_reference", sa.String(255), nullable=True))

    op.create_unique_constraint("uq_users_tracking_email", "users", ["tracking_email"])
    op.create_index("ix_users_tracking_email", "users", ["tracking_email"])
    op.create_index("ix_users_magic_link_token", "users", ["magic_link_token"])
    op.create_unique_constraint("uq_users_auth_token", "users", ["auth_token"])
    op.create_index("ix_users_auth_token", "users", ["auth_token"])


def downgrade() -> None:
    op.drop_index("ix_users_auth_token", table_name="users")
    op.drop_constraint("uq_users_auth_token", "users", type_="unique")
    op.drop_index("ix_users_magic_link_token", table_name="users")
    op.drop_index("ix_users_tracking_email", table_name="users")
    op.drop_constraint("uq_users_tracking_email", "users", type_="unique")

    op.drop_column("users", "payment_reference")
    op.drop_column("users", "payment_session_id")
    op.drop_column("users", "auth_token")
    op.drop_column("users", "token_expires_at")
    op.drop_column("users", "magic_link_token")
    op.drop_column("users", "subscription_status")
    op.drop_column("users", "tracking_email")
    op.drop_column("users", "company_name")
