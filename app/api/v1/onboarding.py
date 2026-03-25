"""
Onboarding endpoints — post-payment setup for a company account.

POST /api/v1/onboarding/tracking-email   — set the company's inbound tracking email
POST /api/v1/onboarding/whatsapp-phone   — associate a WhatsApp phone number with the company
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_auth_token
from app.db.session import get_db
from app.models.orm import User, UserWhatsAppPhone

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/onboarding", tags=["onboarding"])

DbSessionDep = Annotated[AsyncSession, Depends(get_db)]
CurrentUserDep = Annotated[User, Depends(require_auth_token)]

_TRACKING_DOMAIN = ".track@tydline.com"


class SetTrackingEmailBody(BaseModel):
    tracking_email: str


class SetWhatsAppPhoneBody(BaseModel):
    phone: str


@router.get("/tracking-email/check", status_code=status.HTTP_200_OK)
async def check_tracking_email(
    prefix: Annotated[str, Query(...)],
    db: DbSessionDep,
    current_user: CurrentUserDep,
) -> dict:
    """Check whether a tracking email prefix is available."""
    normalised = f"{prefix.strip().lower()}{_TRACKING_DOMAIN}"
    result = await db.execute(
        select(User).where(
            User.tracking_email == normalised,
            User.id != current_user.id,
        )
    )
    return {"available": result.scalar_one_or_none() is None}


@router.post("/tracking-email", status_code=status.HTTP_200_OK)
async def set_tracking_email(
    payload: SetTrackingEmailBody,
    db: DbSessionDep,
    current_user: CurrentUserDep,
) -> dict:
    """
    Set the company's inbound tracking email address.
    The address must be in the form *@track.tydline.com and must be unique.
    Requires an active subscription.
    """
    if current_user.subscription_status != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="An active subscription is required to set a tracking email",
        )

    normalised = payload.tracking_email.strip().lower()
    if not normalised.endswith(_TRACKING_DOMAIN):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Tracking email must end with {_TRACKING_DOMAIN} (e.g. yourcompany{_TRACKING_DOMAIN})",
        )

    payload.tracking_email = normalised

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


@router.get("/whatsapp-phone", status_code=status.HTTP_200_OK)
async def get_whatsapp_phones(
    db: DbSessionDep,
    current_user: CurrentUserDep,
) -> dict:
    """Return all WhatsApp phone numbers associated with this account."""
    result = await db.execute(
        select(UserWhatsAppPhone).where(UserWhatsAppPhone.user_id == current_user.id)
    )
    phones = [row.phone for row in result.scalars().all()]
    return {"phones": phones}


@router.post("/whatsapp-phone", status_code=status.HTTP_200_OK)
async def add_whatsapp_phone(
    payload: SetWhatsAppPhoneBody,
    db: DbSessionDep,
    current_user: CurrentUserDep,
) -> dict:
    """
    Add a WhatsApp phone number to this company account.
    The number must be in international format without '+' (e.g. 233XXXXXXXXX).
    Multiple numbers can be associated with one account.
    """
    normalised = payload.phone.strip().lstrip("+")

    # Uniqueness check — each phone can only belong to one company
    result = await db.execute(
        select(UserWhatsAppPhone).where(UserWhatsAppPhone.phone == normalised)
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        if existing.user_id == current_user.id:
            # Already registered to this user — return success
            phones_result = await db.execute(
                select(UserWhatsAppPhone).where(UserWhatsAppPhone.user_id == current_user.id)
            )
            phones = [row.phone for row in phones_result.scalars().all()]
            return {"user_id": str(current_user.id), "phones": phones}
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This phone number is already associated with another account",
        )

    db.add(UserWhatsAppPhone(user_id=current_user.id, phone=normalised))
    await db.commit()

    phones_result = await db.execute(
        select(UserWhatsAppPhone).where(UserWhatsAppPhone.user_id == current_user.id)
    )
    phones = [row.phone for row in phones_result.scalars().all()]
    return {"user_id": str(current_user.id), "phones": phones}


@router.delete("/whatsapp-phone/{phone}", status_code=status.HTTP_200_OK)
async def remove_whatsapp_phone(
    phone: str,
    db: DbSessionDep,
    current_user: CurrentUserDep,
) -> dict:
    """Remove a WhatsApp phone number from this account."""
    normalised = phone.strip().lstrip("+")
    result = await db.execute(
        select(UserWhatsAppPhone).where(
            UserWhatsAppPhone.phone == normalised,
            UserWhatsAppPhone.user_id == current_user.id,
        )
    )
    entry = result.scalar_one_or_none()
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Phone number not found")

    await db.delete(entry)
    await db.commit()
    return {"removed": normalised}
