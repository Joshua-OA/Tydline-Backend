"""add otp fields to users

Revision ID: p5e6f7a8b9c0
Revises: o4d5e6f7a8b9
Create Date: 2026-03-27 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'p5e6f7a8b9c0'
down_revision = 'o4d5e6f7a8b9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('otp_code', sa.String(255), nullable=True))
    op.add_column('users', sa.Column('otp_expires_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'otp_expires_at')
    op.drop_column('users', 'otp_code')
