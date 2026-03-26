"""clear all data except coupons

Revision ID: o4d5e6f7a8b9
Revises: n3c4d5e6f7a8
Create Date: 2026-03-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "o4d5e6f7a8b9"
down_revision: Union[str, None] = "n3c4d5e6f7a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Truncate all user-data tables. CASCADE handles FK-dependent children:
    #   shipments → shipment_events, notifications, risk_alerts
    #   users     → notify_parties, user_whatsapp_phones, user_authorized_emails
    # inbound_emails and ai_generated_messages are listed explicitly because
    # their FK to users is SET NULL (not CASCADE).
    op.execute("""
        TRUNCATE TABLE
            users,
            inbound_emails,
            ai_generated_messages,
            risk_alerts
        CASCADE
    """)


def downgrade() -> None:
    # Data deletion is not reversible.
    pass
