"""
Magic-link authentication service.

Flow:
  1. generate_magic_link  — create token, hash, store on user, return link URL
  2. verify_magic_link    — verify token hash + expiry, clear token, issue auth_token
  3. get_user_by_auth_token — used as FastAPI dependency to identify callers
"""

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.orm import User

logger = logging.getLogger(__name__)

_TOKEN_EXPIRY_MINUTES = 30


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def generate_magic_link(user: User, db: AsyncSession) -> str:
    """
    Create a one-time magic link token, persist its hash + expiry on *user*,
    and return the full verification URL.
    """
    raw_token = secrets.token_urlsafe(32)
    user.magic_link_token = _hash_token(raw_token)
    user.token_expires_at = datetime.now(tz=timezone.utc) + timedelta(minutes=_TOKEN_EXPIRY_MINUTES)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    url = f"{settings.frontend_url}/auth/verify?token={raw_token}"
    logger.debug("Magic link generated for user %s", user.id)
    return url


async def verify_magic_link(token: str, db: AsyncSession) -> User | None:
    """
    Validate *token*, clear token fields, and issue (or reuse) an auth_token.
    Returns the User on success, None on failure.
    """
    token_hash = _hash_token(token)
    result = await db.execute(select(User).where(User.magic_link_token == token_hash))
    user = result.scalar_one_or_none()

    if user is None:
        logger.info("Magic link verification failed — token not found")
        return None

    if user.token_expires_at is None or datetime.now(tz=timezone.utc) > user.token_expires_at:
        logger.info("Magic link verification failed — token expired for user %s", user.id)
        # Clear stale token
        user.magic_link_token = None
        user.token_expires_at = None
        db.add(user)
        await db.commit()
        return None

    # Token is valid — clear it and issue auth_token if not already set
    user.magic_link_token = None
    user.token_expires_at = None
    if not user.auth_token:
        user.auth_token = secrets.token_urlsafe(32)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    logger.info("Magic link verified for user %s", user.id)
    return user


async def get_user_by_auth_token(token: str, db: AsyncSession) -> User | None:
    """Look up a user by their Bearer auth_token."""
    result = await db.execute(select(User).where(User.auth_token == token))
    return result.scalar_one_or_none()
