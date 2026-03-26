"""
Magic-link authentication endpoints.

POST /api/v1/auth/request-link  — request a magic link (upsert user by email)
GET  /api/v1/auth/verify        — exchange token for HttpOnly session cookie
POST /api/v1/auth/logout        — clear the session cookie
"""

import base64
import json
import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_db
from app.models.orm import User
from app.services.auth import generate_magic_link, user_id_from_email, verify_magic_link
from app.services.email import send_email

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

DbSessionDep = Annotated[AsyncSession, Depends(get_db)]

_COOKIE_NAME = "tydline_auth"
_COOKIE_MAX_AGE = 60 * 60 * 24 * 7  # 7 days


class RequestLinkBody(BaseModel):
    email: EmailStr
    company_name: str
    metadata: dict | None = None


@router.post("/request-link", status_code=status.HTTP_200_OK)
async def request_magic_link(request: Request, payload: RequestLinkBody, db: DbSessionDep) -> dict:
    """
    Upsert user by email, generate a magic link, and send it via Postmark.
    Always returns 200 — never reveals whether the email already existed.
    """
    origin = request.headers.get("origin", "")
    logger.info("request-link: email=%s origin=%r", payload.email, origin or "(none)")

    # Atomic upsert with deterministic ID — same email always gets the same UUID.
    # Avoids TOCTOU race conditions and makes user_id predictable from email.
    uid = user_id_from_email(payload.email)
    stmt = (
        pg_insert(User)
        .values(id=uid, email=payload.email, company_name=payload.company_name)
        .on_conflict_do_update(
            index_elements=["email"],
            set_={"company_name": payload.company_name},
        )
        .returning(User)
    )
    result = await db.execute(stmt)
    user = result.scalar_one()
    await db.flush()
    logger.info("request-link: upserted user %s email=%s", user.id, user.email)

    frontend_url = origin if "localhost" in origin else None
    logger.info("request-link: magic link will point to %s", frontend_url or settings.frontend_url)
    link = await generate_magic_link(user, db, frontend_url=frontend_url)

    if payload.metadata:
        state = base64.urlsafe_b64encode(
            json.dumps(payload.metadata, separators=(",", ":")).encode()
        ).decode()
        link = f"{link}&state={state}"

    text_body = (
        f"Hi,\n\n"
        f"Click the link below to sign in to Tydline. "
        f"This link expires in 15 minutes.\n\n"
        f"{link}\n\n"
        f"If you did not request this, you can safely ignore this email.\n\n"
        f"— The Tydline Team"
    )

    html_body: str | None = None
    template_path = Path(__file__).parents[3] / "emails" / "magic-link.html"
    try:
        html_body = template_path.read_text(encoding="utf-8")
        html_body = html_body.replace("{{email}}", payload.email).replace("{{magic_link}}", link)
    except Exception:
        logger.warning("request-link: could not load magic-link email template from %s", template_path)

    await send_email(
        to=payload.email,
        subject="Your Tydline sign-in link",
        text_body=text_body,
        html_body=html_body,
    )
    logger.info("request-link: magic link email sent to %s", payload.email)

    return {"message": "Check your email"}


@router.get("/verify", status_code=status.HTTP_200_OK)
async def verify_token(
    request: Request,
    response: Response,
    token: Annotated[str, Query(...)],
    db: DbSessionDep,
    tydline_auth: Annotated[str | None, Cookie()] = None,
) -> dict:
    """
    Exchange a magic-link token for a session cookie.
    Sets an HttpOnly cookie — frontend receives user_id + subscription_status
    but never sees the raw auth_token value.
    """
    from app.services.auth import get_user_by_auth_token

    origin = request.headers.get("origin", "")
    is_localhost = "localhost" in origin
    logger.info("verify: origin=%r is_localhost=%s existing_cookie=%s", origin or "(none)", is_localhost, bool(tydline_auth))

    user = await verify_magic_link(token, db)

    if user is None:
        logger.warning("verify: token invalid or expired")
        # Token already consumed — check if this session is already authenticated
        # (handles double-invocation from React StrictMode / duplicate requests)
        if tydline_auth:
            existing = await get_user_by_auth_token(tydline_auth, db)
            if existing:
                logger.info("verify: reusing existing session for user %s", existing.id)
                return {
                    "user_id": str(existing.id),
                    "subscription_status": existing.subscription_status,
                }
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

    # Cross-site (localhost → api.tydline.com) requires SameSite=None + Secure.
    # Same-site production traffic (tydline.com → api.tydline.com) uses Lax.
    samesite = "none" if is_localhost else "lax"
    logger.info("verify: user %s authenticated — setting cookie secure=True samesite=%s", user.id, samesite)
    response.set_cookie(
        key=_COOKIE_NAME,
        value=user.auth_token,
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        secure=True,
        samesite=samesite,
    )
    return {
        "user_id": str(user.id),
        "subscription_status": user.subscription_status,
    }


@router.post("/logout", status_code=status.HTTP_200_OK)
async def logout(request: Request, response: Response) -> dict:
    """Clear the session cookie."""
    origin = request.headers.get("origin", "")
    is_localhost = "localhost" in origin

    response.delete_cookie(
        key=_COOKIE_NAME,
        httponly=True,
        secure=True,
        samesite="none" if is_localhost else "lax",
    )
    return {"message": "Logged out"}


