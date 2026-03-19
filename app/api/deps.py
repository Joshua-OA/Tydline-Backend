"""
Shared FastAPI dependencies (auth, etc.).
"""

from typing import Annotated

from fastapi import Cookie, Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_db


async def require_api_key(x_api_key: str | None = Header(None, alias="X-API-Key")) -> None:
    """
    If API_KEY is set in env, require X-API-Key header to match.
    If API_KEY is not set, skip validation (allow all).
    """
    if not settings.api_key:
        return
    if not x_api_key or x_api_key != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )


async def require_auth_token(
    tydline_auth: Annotated[str | None, Cookie()] = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Validate the HttpOnly session cookie set by GET /auth/verify.
    Returns the authenticated User or raises 401.
    """
    from app.services.auth import get_user_by_auth_token

    if not tydline_auth:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    user = await get_user_by_auth_token(tydline_auth, db)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session",
        )
    return user
