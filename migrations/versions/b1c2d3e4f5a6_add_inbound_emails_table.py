"""add inbound_emails table

Revision ID: b1c2d3e4f5a6
Revises: a3b7c9d1e2f4
Create Date: 2026-03-18 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, Sequence[str], None] = "a3b7c9d1e2f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "inbound_emails",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("from_email", sa.String(255), nullable=False),
        sa.Column("from_name", sa.String(255), nullable=True),
        sa.Column("to_email", sa.String(512), nullable=False),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("body_text", sa.Text(), nullable=True),
        sa.Column("body_html", sa.Text(), nullable=True),
        sa.Column("message_id", sa.String(255), nullable=True),
        sa.Column("container_numbers", sa.JSON(), nullable=True),
        sa.Column("bl_numbers", sa.JSON(), nullable=True),
        sa.Column("carrier", sa.String(128), nullable=True),
        sa.Column("email_summary", sa.Text(), nullable=True),
        sa.Column("matched_shipment_ids", sa.JSON(), nullable=True),
        sa.Column("mem0_stored", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_inbound_emails_user_id", "inbound_emails", ["user_id"])
    op.create_index("ix_inbound_emails_from_email", "inbound_emails", ["from_email"])
    op.create_unique_constraint(
        "uq_inbound_emails_message_id", "inbound_emails", ["message_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_inbound_emails_message_id", "inbound_emails", type_="unique")
    op.drop_index("ix_inbound_emails_from_email", table_name="inbound_emails")
    op.drop_index("ix_inbound_emails_user_id", table_name="inbound_emails")
    op.drop_table("inbound_emails")
