"""
Onboarding endpoints — post-payment setup for a company account.

POST /api/v1/onboarding/tracking-email  — set the company's inbound tracking email
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_auth_token
from app.db.session import get_db
from app.models.orm import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/onboarding", tags=["onboarding"])

DbSessionDep = Annotated[AsyncSession, Depends(get_db)]
CurrentUserDep = Annotated[User, Depends(require_auth_token)]

_TRACKING_DOMAIN = "@track.tydline.com"


class SetTrackingEmailBody(BaseModel):
    tracking_email: str

    @field_validator("tracking_email")
    @classmethod
    def validate_tracking_email(cls, v: str) -> str:
        normalised = v.strip().lower()
        if not normalised.endswith(_TRACKING_DOMAIN):
            raise ValueError(f"tracking_email must end with {_TRACKING_DOMAIN}")
        return normalised


@router.post("/tracking-email", status_code=status.HTTP_200_OK)
async def set_tracking_email(
    payload: SetTrackingEmailBody,
    db: DbSessionDep,
    current_user: CurrentUserDep,
) -> dict:
    """
    Set the company's inbound tracking email address.
    The address must be in the form *@track.tydline.com and must be unique.
    """
    # Uniqueness check (exclude the current user so they can re-set their own)
    result = await db.execute(
        select(User).where(
            User.tracking_email == payload.tracking_email,
            User.id != current_user.id,
        )
    )
    if result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This tracking email is already in use",
        )

    current_user.tracking_email = payload.tracking_email
    db.add(current_user)
    await db.commit()
    await db.refresh(current_user)

    return {
        "user_id": str(current_user.id),
        "tracking_email": current_user.tracking_email,
        "subscription_status": current_user.subscription_status,
    }
