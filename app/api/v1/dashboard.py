"""
Dashboard endpoints — cookie-authenticated, automatically scoped to the current user.

GET /api/v1/dashboard/shipments          — all shipments split into active / completed
GET /api/v1/dashboard/shipments/active   — only in-progress shipments
GET /api/v1/dashboard/shipments/completed — only terminal shipments
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_auth_token
from app.db.session import get_db
from app.models.orm import Shipment, User
from app.schemas.shipment import ShipmentRead

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

DbSessionDep = Annotated[AsyncSession, Depends(get_db)]
CurrentUserDep = Annotated[User, Depends(require_auth_token)]

# Statuses that mean a shipment has finished moving
_COMPLETED_STATUSES = {"arrived", "delivered", "completed"}


def _is_completed(shipment: Shipment) -> bool:
    return (shipment.status or "").lower() in _COMPLETED_STATUSES


class DashboardShipmentsResponse(BaseModel):
    active: list[ShipmentRead]
    completed: list[ShipmentRead]
    total_active: int
    total_completed: int

    model_config = ConfigDict(from_attributes=True)


async def _get_user_shipments(user: User, db: AsyncSession) -> list[Shipment]:
    result = await db.execute(
        select(Shipment)
        .where(Shipment.user_id == user.id)
        .order_by(Shipment.created_at.desc())
    )
    return list(result.scalars().all())


@router.get("/shipments", response_model=DashboardShipmentsResponse)
async def dashboard_shipments(
    db: DbSessionDep,
    current_user: CurrentUserDep,
) -> DashboardShipmentsResponse:
    """All shipments for the logged-in company, split into active and completed."""
    all_shipments = await _get_user_shipments(current_user, db)
    active = [s for s in all_shipments if not _is_completed(s)]
    completed = [s for s in all_shipments if _is_completed(s)]
    return DashboardShipmentsResponse(
        active=[ShipmentRead.model_validate(s) for s in active],
        completed=[ShipmentRead.model_validate(s) for s in completed],
        total_active=len(active),
        total_completed=len(completed),
    )


@router.get("/shipments/active", response_model=list[ShipmentRead])
async def active_shipments(
    db: DbSessionDep,
    current_user: CurrentUserDep,
) -> list[ShipmentRead]:
    """Shipments currently being tracked (not yet arrived/delivered)."""
    all_shipments = await _get_user_shipments(current_user, db)
    return [ShipmentRead.model_validate(s) for s in all_shipments if not _is_completed(s)]


@router.get("/shipments/completed", response_model=list[ShipmentRead])
async def completed_shipments(
    db: DbSessionDep,
    current_user: CurrentUserDep,
) -> list[ShipmentRead]:
    """Shipments that have arrived or been delivered."""
    all_shipments = await _get_user_shipments(current_user, db)
    return [ShipmentRead.model_validate(s) for s in all_shipments if _is_completed(s)]
