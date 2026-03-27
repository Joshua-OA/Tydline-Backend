"""
Magic-link authentication service.

Flow:
  1. generate_magic_link  — create token, hash, store on user, return link URL
  2. verify_magic_link    — verify token hash + expiry, clear token, issue auth_token
  3. get_user_by_auth_token — used as FastAPI dependency to identify callers
"""

import hashlib
import logging
import random
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.orm import User

logger = logging.getLogger(__name__)

_TOKEN_EXPIRY_MINUTES = 30

# Fixed namespace for deterministic user IDs derived from email.
# UUID5(namespace, email) always produces the same UUID for the same email.
_USER_ID_NAMESPACE = uuid.UUID("6ba7b814-9dad-11d1-80b4-00c04fd430c8")


def user_id_from_email(email: str) -> uuid.UUID:
    """Return a deterministic UUID5 for *email* (lowercased + stripped)."""
    return uuid.uuid5(_USER_ID_NAMESPACE, email.lower().strip())


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def generate_magic_link(user: User, db: AsyncSession, frontend_url: str | None = None) -> str:
    """
    Create a one-time magic link token, persist its hash + expiry on *user*,
    and return the full verification URL.

    frontend_url overrides settings.frontend_url — pass the request Origin so
    magic links sent from localhost point back to localhost.
    """
    raw_token = secrets.token_urlsafe(32)
    user.magic_link_token = _hash_token(raw_token)
    user.token_expires_at = datetime.now(tz=timezone.utc) + timedelta(minutes=_TOKEN_EXPIRY_MINUTES)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    base = (frontend_url or settings.frontend_url).rstrip("/")
    url = f"{base}/auth/verify?token={raw_token}"
    logger.info(
        "generate_magic_link: user_id=%s email=%s token_expires_at=%s link_base=%s",
        user.id, user.email, user.token_expires_at.isoformat(), base,
    )
    return url


async def verify_magic_link(token: str, db: AsyncSession) -> User | None:
    """
    Validate *token*, clear token fields, and issue (or reuse) an auth_token.
    Returns the User on success, None on failure.
    """
    token_hash = _hash_token(token)
    logger.info("verify_magic_link: looking up token hash %s…", token_hash[:12])
    result = await db.execute(select(User).where(User.magic_link_token == token_hash))
    user = result.scalar_one_or_none()

    if user is None:
        logger.warning("verify_magic_link: no user found for token hash %s…", token_hash[:12])
        return None

    logger.info(
        "verify_magic_link: found user_id=%s email=%s token_expires_at=%s",
        user.id, user.email, user.token_expires_at,
    )

    now = datetime.now(tz=timezone.utc)
    if user.token_expires_at is None or now > user.token_expires_at:
        logger.warning(
            "verify_magic_link: token expired for user_id=%s email=%s (expired=%s now=%s)",
            user.id, user.email, user.token_expires_at, now.isoformat(),
        )
        user.magic_link_token = None
        user.token_expires_at = None
        db.add(user)
        await db.commit()
        return None

    had_auth_token = bool(user.auth_token)
    user.magic_link_token = None
    user.token_expires_at = None
    if not user.auth_token:
        user.auth_token = secrets.token_urlsafe(32)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    logger.info(
        "verify_magic_link: success user_id=%s email=%s reused_auth_token=%s",
        user.id, user.email, had_auth_token,
    )
    return user


async def generate_otp(user: User, db: AsyncSession) -> str:
    """
    Generate a 6-digit OTP, persist its hash + expiry on *user*, return the raw code.
    Shares the same expiry window as the magic link token.
    """
    raw_otp = str(random.randint(100_000, 999_999))
    user.otp_code = _hash_token(raw_otp)
    user.otp_expires_at = datetime.now(tz=timezone.utc) + timedelta(minutes=_TOKEN_EXPIRY_MINUTES)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    logger.info("generate_otp: user_id=%s email=%s otp_expires_at=%s", user.id, user.email, user.otp_expires_at.isoformat())
    return raw_otp


async def verify_otp(email: str, otp: str, db: AsyncSession) -> User | None:
    """
    Validate *otp* for *email*, clear OTP fields, and issue (or reuse) an auth_token.
    Returns the User on success, None on failure.
    """
    otp_hash = _hash_token(otp)
    result = await db.execute(select(User).where(User.email == email.lower().strip()))
    user = result.scalar_one_or_none()

    if user is None:
        logger.warning("verify_otp: no user found for email %s", email)
        return None

    if user.otp_code is None or user.otp_code != otp_hash:
        logger.warning("verify_otp: otp mismatch for user_id=%s", user.id)
        return None

    now = datetime.now(tz=timezone.utc)
    if user.otp_expires_at is None or now > user.otp_expires_at:
        logger.warning("verify_otp: otp expired for user_id=%s (expired=%s now=%s)", user.id, user.otp_expires_at, now.isoformat())
        user.otp_code = None
        user.otp_expires_at = None
        db.add(user)
        await db.commit()
        return None

    user.otp_code = None
    user.otp_expires_at = None
    if not user.auth_token:
        user.auth_token = secrets.token_urlsafe(32)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    logger.info("verify_otp: success user_id=%s email=%s", user.id, user.email)
    return user


async def get_user_by_auth_token(token: str, db: AsyncSession) -> User | None:
    """Look up a user by their Bearer auth_token."""
    result = await db.execute(select(User).where(User.auth_token == token))
    user = result.scalar_one_or_none()
    if user is None:
        logger.warning("get_user_by_auth_token: no user found for token %s…", token[:8])
    else:
        logger.info("get_user_by_auth_token: user_id=%s email=%s", user.id, user.email)
    return user
