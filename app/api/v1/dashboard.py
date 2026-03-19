"""
Dashboard endpoints — cookie-authenticated, automatically scoped to the current user.

GET  /api/v1/dashboard/shipments                        — all shipments split into pending / active / completed
GET  /api/v1/dashboard/shipments/active                 — only in-progress shipments
GET  /api/v1/dashboard/shipments/completed              — only terminal shipments
GET  /api/v1/dashboard/approvals                        — shipments awaiting approval
POST /api/v1/dashboard/shipments/submit                 — submit a shipment for tracking (pending_approval)
POST /api/v1/dashboard/approvals/{shipment_id}/approve  — manually approve before 3-day auto-approval
"""

import re
import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_auth_token
from app.db.session import get_db
from app.models.orm import Shipment, User
from app.schemas.shipment import ShipmentRead
from app.services.tracking import initial_track_shipment

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

DbSessionDep = Annotated[AsyncSession, Depends(get_db)]
CurrentUserDep = Annotated[User, Depends(require_auth_token)]

_COMPLETED_STATUSES = {"arrived", "delivered", "completed"}
_CONTAINER_RE = re.compile(r"^[A-Z]{4}\d{7}$")


def _is_completed(shipment: Shipment) -> bool:
    return (shipment.status or "").lower() in _COMPLETED_STATUSES


def _is_pending(shipment: Shipment) -> bool:
    return (shipment.status or "").lower() == "pending_approval"


class DashboardShipmentsResponse(BaseModel):
    pending_approval: list[ShipmentRead]
    active: list[ShipmentRead]
    completed: list[ShipmentRead]
    total_pending_approval: int
    total_active: int
    total_completed: int

    model_config = ConfigDict(from_attributes=True)


class ShipmentSubmit(BaseModel):
    container_number: str
    bill_of_lading: str | None = None
    carrier: str | None = None

    @field_validator("container_number")
    @classmethod
    def validate_container_number(cls, v: str) -> str:
        normalised = v.strip().upper()
        if not _CONTAINER_RE.match(normalised):
            raise ValueError(
                "container_number must be 4 letters followed by 7 digits (e.g. MSCU1234567)"
            )
        return normalised


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
    """All shipments split into pending_approval, active, and completed."""
    all_shipments = await _get_user_shipments(current_user, db)
    pending = [s for s in all_shipments if _is_pending(s)]
    active = [s for s in all_shipments if not _is_pending(s) and not _is_completed(s)]
    completed = [s for s in all_shipments if _is_completed(s)]
    return DashboardShipmentsResponse(
        pending_approval=[ShipmentRead.model_validate(s) for s in pending],
        active=[ShipmentRead.model_validate(s) for s in active],
        completed=[ShipmentRead.model_validate(s) for s in completed],
        total_pending_approval=len(pending),
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
    return [ShipmentRead.model_validate(s) for s in all_shipments if not _is_pending(s) and not _is_completed(s)]


@router.get("/shipments/completed", response_model=list[ShipmentRead])
async def completed_shipments(
    db: DbSessionDep,
    current_user: CurrentUserDep,
) -> list[ShipmentRead]:
    """Shipments that have arrived or been delivered."""
    all_shipments = await _get_user_shipments(current_user, db)
    return [ShipmentRead.model_validate(s) for s in all_shipments if _is_completed(s)]


@router.get("/approvals", response_model=list[ShipmentRead])
async def list_approvals(
    db: DbSessionDep,
    current_user: CurrentUserDep,
) -> list[ShipmentRead]:
    """Shipments awaiting approval before tracking begins."""
    all_shipments = await _get_user_shipments(current_user, db)
    return [ShipmentRead.model_validate(s) for s in all_shipments if _is_pending(s)]


@router.post("/shipments/submit", response_model=ShipmentRead, status_code=status.HTTP_201_CREATED)
async def submit_shipment(
    payload: ShipmentSubmit,
    db: DbSessionDep,
    current_user: CurrentUserDep,
) -> ShipmentRead:
    """
    Submit a shipment for tracking.
    Creates it with pending_approval status — tracking begins after 3 days
    or immediately on manual approval.
    """
    shipment = Shipment(
        container_number=payload.container_number,
        bill_of_lading=payload.bill_of_lading,
        carrier=payload.carrier,
        user_id=current_user.id,
        status="pending_approval",
    )
    db.add(shipment)
    await db.commit()
    await db.refresh(shipment)
    return ShipmentRead.model_validate(shipment)


@router.post("/approvals/{shipment_id}/approve", response_model=ShipmentRead)
async def approve_shipment(
    shipment_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    db: DbSessionDep,
    current_user: CurrentUserDep,
) -> ShipmentRead:
    """
    Manually approve a pending shipment before the 3-day auto-approval window.
    Kicks off tracking immediately.
    """
    result = await db.execute(
        select(Shipment).where(
            Shipment.id == shipment_id,
            Shipment.user_id == current_user.id,
        )
    )
    shipment = result.scalar_one_or_none()
    if shipment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Shipment not found")
    if shipment.status != "pending_approval":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Shipment is not pending approval",
        )

    shipment.status = "tracking_started"
    db.add(shipment)
    await db.commit()
    await db.refresh(shipment)

    background_tasks.add_task(initial_track_shipment, shipment.id)

    return ShipmentRead.model_validate(shipment)
