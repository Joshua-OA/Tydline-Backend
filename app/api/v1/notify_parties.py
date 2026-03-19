"""
Notify-party endpoints — manage contacts that receive shipment notifications.

GET    /api/v1/notify-parties        — list all notify parties for the company
POST   /api/v1/notify-parties        — add a notify party
DELETE /api/v1/notify-parties/{id}   — remove a notify party

Plan constraints:
  Starter  — single channel only (email OR whatsapp, not both)
  Growth+  — email + whatsapp simultaneously
  All plans require an active subscription to add WhatsApp contacts.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_auth_token
from app.core.plans import get_test_features, get_user_features, is_test_account
from app.db.session import get_db
from app.models.orm import NotifyParty, User

router = APIRouter(prefix="/notify-parties", tags=["notify-parties"])

DbSessionDep = Annotated[AsyncSession, Depends(get_db)]
CurrentUserDep = Annotated[User, Depends(require_auth_token)]

_VALID_CHANNELS = {"email", "whatsapp"}


class NotifyPartyCreate(BaseModel):
    name: str
    channel: str
    contact_value: str

    @field_validator("channel")
    @classmethod
    def validate_channel(cls, v: str) -> str:
        v = v.lower()
        if v not in _VALID_CHANNELS:
            raise ValueError(f"channel must be one of: {', '.join(_VALID_CHANNELS)}")
        return v


class NotifyPartyRead(BaseModel):
    id: uuid.UUID
    name: str
    channel: str
    contact_value: str

    model_config = ConfigDict(from_attributes=True)


@router.get("", response_model=list[NotifyPartyRead])
async def list_notify_parties(
    db: DbSessionDep,
    current_user: CurrentUserDep,
) -> list[NotifyPartyRead]:
    """List all notify parties for the logged-in company."""
    result = await db.execute(
        select(NotifyParty)
        .where(NotifyParty.user_id == current_user.id)
        .order_by(NotifyParty.created_at.asc())
    )
    return [NotifyPartyRead.model_validate(p) for p in result.scalars().all()]


@router.post("", response_model=NotifyPartyRead, status_code=status.HTTP_201_CREATED)
async def add_notify_party(
    payload: NotifyPartyCreate,
    db: DbSessionDep,
    current_user: CurrentUserDep,
) -> NotifyPartyRead:
    """
    Add a notify party.

    - Requires an active subscription for WhatsApp contacts.
    - Starter plan: only one channel allowed across all notify parties.
    - Growth / Pro: email + WhatsApp simultaneously.
    """
    features = get_test_features() if is_test_account(current_user) else get_user_features(current_user.plan, current_user.subscription_status)

    if features is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="An active subscription is required to add notify parties",
        )

    if payload.channel == "whatsapp" and not features.whatsapp_notifications:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="WhatsApp notifications are not available on your plan",
        )

    # Starter plan: enforce single-channel constraint
    if not features.multi_channel:
        result = await db.execute(
            select(NotifyParty).where(
                NotifyParty.user_id == current_user.id,
                NotifyParty.channel != payload.channel,
            )
        )
        if result.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Your plan only allows a single notification channel. "
                    "Remove your existing notify parties before switching channels, "
                    "or upgrade to Growth for email + WhatsApp."
                ),
            )

    party = NotifyParty(
        user_id=current_user.id,
        name=payload.name,
        channel=payload.channel,
        contact_value=payload.contact_value,
    )
    db.add(party)
    await db.commit()
    await db.refresh(party)
    return NotifyPartyRead.model_validate(party)


@router.delete("/{party_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_notify_party(
    party_id: uuid.UUID,
    db: DbSessionDep,
    current_user: CurrentUserDep,
) -> None:
    """Remove a notify party. Only the owning company can delete their own contacts."""
    result = await db.execute(
        select(NotifyParty).where(
            NotifyParty.id == party_id,
            NotifyParty.user_id == current_user.id,
        )
    )
    party = result.scalar_one_or_none()
    if party is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notify party not found")
    await db.delete(party)
    await db.commit()
