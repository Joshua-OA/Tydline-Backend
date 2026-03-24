"""
Admin coupon management endpoints.

POST /api/v1/coupons        — create a coupon (API key required)
GET  /api/v1/coupons        — list all coupons (API key required)
DELETE /api/v1/coupons/{id} — deactivate a coupon (API key required)
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_api_key
from app.core.plans import PLANS
from app.db.session import get_db
from app.models.orm import Coupon

router = APIRouter(
    prefix="/coupons",
    tags=["coupons"],
    dependencies=[Depends(require_api_key)],
)

DbSessionDep = Annotated[AsyncSession, Depends(get_db)]


class CouponCreate(BaseModel):
    code: str
    plan: str
    max_uses: int | None = None
    expires_at: datetime | None = None


class CouponRead(BaseModel):
    id: uuid.UUID
    code: str
    plan: str
    is_active: bool
    max_uses: int | None
    uses_count: int
    expires_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


@router.post("", response_model=CouponRead, status_code=status.HTTP_201_CREATED)
async def create_coupon(payload: CouponCreate, db: DbSessionDep) -> CouponRead:
    """Create a new coupon code."""
    code = payload.code.strip().upper()
    if not code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="code is required")

    if payload.plan not in PLANS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"plan must be one of: {', '.join(sorted(PLANS))}",
        )

    existing = await db.execute(select(Coupon).where(Coupon.code == code))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Coupon code already exists")

    coupon = Coupon(
        code=code,
        plan=payload.plan,
        max_uses=payload.max_uses,
        expires_at=payload.expires_at,
    )
    db.add(coupon)
    await db.commit()
    await db.refresh(coupon)
    return CouponRead.model_validate(coupon)


@router.get("", response_model=list[CouponRead])
async def list_coupons(db: DbSessionDep) -> list[CouponRead]:
    """List all coupons."""
    result = await db.execute(select(Coupon).order_by(Coupon.created_at.desc()))
    return [CouponRead.model_validate(c) for c in result.scalars().all()]


@router.delete("/{coupon_id}", status_code=status.HTTP_200_OK)
async def deactivate_coupon(coupon_id: uuid.UUID, db: DbSessionDep) -> dict:
    """Deactivate a coupon so it can no longer be redeemed."""
    result = await db.execute(select(Coupon).where(Coupon.id == coupon_id))
    coupon = result.scalar_one_or_none()
    if coupon is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Coupon not found")

    coupon.is_active = False
    db.add(coupon)
    await db.commit()
    return {"id": str(coupon_id), "is_active": False}
