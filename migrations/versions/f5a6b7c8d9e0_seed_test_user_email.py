"""seed test user email for joshuaobeng

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
Create Date: 2026-03-19 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f5a6b7c8d9e0'
down_revision: Union[str, Sequence[str], None] = 'e4f5a6b7c8d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Link joshuaobeng.a@gmail.com to the leleimports test user for inbound email matching."""
    op.execute(
        """
        UPDATE users
        SET email = 'joshuaobeng.a@gmail.com'
        WHERE tracking_email = 'leleimports@track.tydline.com'
          AND (email IS NULL OR email != 'joshuaobeng.a@gmail.com')
        """
    )


def downgrade() -> None:
    """Revert — clear the email on the test user."""
    op.execute(
        """
        UPDATE users
        SET email = NULL
        WHERE tracking_email = 'leleimports@track.tydline.com'
          AND email = 'joshuaobeng.a@gmail.com'
        """
    )
