"""add user_authorized_emails table

Revision ID: i8d9e0f1a2b3
Revises: h7c8d9e0f1a2
Create Date: 2026-03-19 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = 'i8d9e0f1a2b3'
down_revision: Union[str, Sequence[str], None] = 'h7c8d9e0f1a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'user_authorized_emails',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('user_id', UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('email', sa.String(255), nullable=False, unique=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_user_authorized_emails_user_id', 'user_authorized_emails', ['user_id'])
    op.create_index('ix_user_authorized_emails_email', 'user_authorized_emails', ['email'], unique=True)

    # Seed joshuaobeng.a@gmail.com as an authorized sender for the leleimports test account
    op.execute(
        """
        INSERT INTO user_authorized_emails (id, user_id, email)
        SELECT gen_random_uuid(), id, 'joshuaobeng.a@gmail.com'
        FROM users WHERE tracking_email = 'leleimports@track.tydline.com'
        ON CONFLICT (email) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_index('ix_user_authorized_emails_email', table_name='user_authorized_emails')
    op.drop_index('ix_user_authorized_emails_user_id', table_name='user_authorized_emails')
    op.drop_table('user_authorized_emails')
