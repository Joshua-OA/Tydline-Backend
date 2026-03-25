"""add user_whatsapp_phones table

Revision ID: g6b7c8d9e0f1
Revises: f5a6b7c8d9e0
Create Date: 2026-03-19 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = 'g6b7c8d9e0f1'
down_revision: Union[str, Sequence[str], None] = 'f5a6b7c8d9e0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'user_whatsapp_phones',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('user_id', UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('phone', sa.String(32), nullable=False, unique=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_user_whatsapp_phones_user_id', 'user_whatsapp_phones', ['user_id'])
    op.create_index('ix_user_whatsapp_phones_phone', 'user_whatsapp_phones', ['phone'], unique=True)

    # Seed both test phones for the leleimports test account
    op.execute(
        """
        INSERT INTO user_whatsapp_phones (id, user_id, phone)
        SELECT gen_random_uuid(), id, '233552354808'
        FROM users WHERE tracking_email = 'leleimports@track.tydline.com'
        ON CONFLICT (phone) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO user_whatsapp_phones (id, user_id, phone)
        SELECT gen_random_uuid(), id, '233506074801'
        FROM users WHERE tracking_email = 'leleimports@track.tydline.com'
        ON CONFLICT (phone) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_index('ix_user_whatsapp_phones_phone', table_name='user_whatsapp_phones')
    op.drop_index('ix_user_whatsapp_phones_user_id', table_name='user_whatsapp_phones')
    op.drop_table('user_whatsapp_phones')
