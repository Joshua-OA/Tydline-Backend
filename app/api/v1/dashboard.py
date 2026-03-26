"""
Dashboard endpoints — cookie-authenticated, automatically scoped to the current user.

GET  /api/v1/dashboard/shipments                        — all shipments split into pending / active / completed
GET  /api/v1/dashboard/shipments/active                 — only in-progress shipments
GET  /api/v1/dashboard/shipments/completed              — only terminal shipments
GET  /api/v1/dashboard/approvals                        — shipments awaiting approval
POST /api/v1/dashboard/shipments/submit                 — submit a shipment for tracking (pending_approval)
POST /api/v1/dashboard/approvals/{shipment_id}/approve  — manually approve before 3-day auto-approval
"""

import logging
import re
import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_auth_token
from app.db.session import get_db
from app.models.orm import Shipment, User
from app.schemas.shipment import ShipmentRead
from app.services.ocr import extract_bl_from_file
from app.services.tracking import initial_track_shipment

logger = logging.getLogger(__name__)

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
    bill_of_lading: str
    container_number: str | None = None
    carrier: str | None = None

    @field_validator("bill_of_lading")
    @classmethod
    def validate_bill_of_lading(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("bill_of_lading must not be empty")
        if len(v) > 64:
            raise ValueError("bill_of_lading must be 64 characters or fewer")
        return v.upper()

    @field_validator("container_number")
    @classmethod
    def validate_container_number(cls, v: str | None) -> str | None:
        if v is None:
            return None
        normalised = v.strip().upper()
        if not _CONTAINER_RE.match(normalised):
            raise ValueError(
                "container_number must be 4 letters followed by 7 digits (e.g. MSCU1234567)"
            )
        return normalised


class ShipmentSubmitResponse(BaseModel):
    id: uuid.UUID
    status: str


class NotifyMeRequest(BaseModel):
    email: str


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
    logger.info(
        "dashboard_shipments: user_id=%s email=%s pending=%d active=%d completed=%d",
        current_user.id, current_user.email, len(pending), len(active), len(completed),
    )
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
    result = [ShipmentRead.model_validate(s) for s in all_shipments if not _is_pending(s) and not _is_completed(s)]
    logger.info("active_shipments: user_id=%s email=%s count=%d", current_user.id, current_user.email, len(result))
    return result


@router.get("/shipments/completed", response_model=list[ShipmentRead])
async def completed_shipments(
    db: DbSessionDep,
    current_user: CurrentUserDep,
) -> list[ShipmentRead]:
    """Shipments that have arrived or been delivered."""
    all_shipments = await _get_user_shipments(current_user, db)
    result = [ShipmentRead.model_validate(s) for s in all_shipments if _is_completed(s)]
    logger.info("completed_shipments: user_id=%s email=%s count=%d", current_user.id, current_user.email, len(result))
    return result


@router.get("/approvals", response_model=list[ShipmentRead])
async def list_approvals(
    db: DbSessionDep,
    current_user: CurrentUserDep,
) -> list[ShipmentRead]:
    """Shipments awaiting approval before tracking begins."""
    all_shipments = await _get_user_shipments(current_user, db)
    result = [ShipmentRead.model_validate(s) for s in all_shipments if _is_pending(s)]
    logger.info("list_approvals: user_id=%s email=%s count=%d", current_user.id, current_user.email, len(result))
    return result


@router.get("/shipments/{shipment_id}", response_model=ShipmentRead)
async def get_dashboard_shipment(
    shipment_id: uuid.UUID,
    db: DbSessionDep,
    current_user: CurrentUserDep,
) -> ShipmentRead:
    """Fetch a single shipment by ID, scoped to the current user."""
    logger.info(
        "get_dashboard_shipment: user_id=%s email=%s shipment_id=%s",
        current_user.id, current_user.email, shipment_id,
    )
    result = await db.execute(
        select(Shipment).where(
            Shipment.id == shipment_id,
            Shipment.user_id == current_user.id,
        )
    )
    shipment = result.scalar_one_or_none()
    if shipment is None:
        logger.warning(
            "get_dashboard_shipment: not found or not owned — user_id=%s shipment_id=%s",
            current_user.id, shipment_id,
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Shipment not found")
    logger.info(
        "get_dashboard_shipment: returned shipment_id=%s container=%s bl=%s status=%s",
        shipment.id, shipment.container_number, shipment.bill_of_lading, shipment.status,
    )
    return ShipmentRead.model_validate(shipment)


@router.post("/shipments/submit", response_model=ShipmentSubmitResponse, status_code=status.HTTP_201_CREATED)
async def submit_shipment(
    payload: ShipmentSubmit,
    background_tasks: BackgroundTasks,
    db: DbSessionDep,
    current_user: CurrentUserDep,
) -> ShipmentSubmitResponse:
    """
    Submit a shipment for tracking.
    Creates it with tracking_started status and immediately kicks off tracking.
    """
    logger.info(
        "submit_shipment: user_id=%s email=%s bl=%s container=%s carrier=%s",
        current_user.id, current_user.email,
        payload.bill_of_lading, payload.container_number, payload.carrier,
    )
    # Duplicate check: same BL, same container number, or BL field contains a container number
    from sqlalchemy import or_
    dup_filter = [Shipment.bill_of_lading == payload.bill_of_lading]
    if payload.container_number:
        dup_filter.append(Shipment.container_number == payload.container_number)
    # User may enter a container number in the BL field — catch that too
    if _CONTAINER_RE.match(payload.bill_of_lading):
        dup_filter.append(Shipment.container_number == payload.bill_of_lading)
    existing_result = await db.execute(
        select(Shipment).where(
            Shipment.user_id == current_user.id,
            or_(*dup_filter),
        )
    )
    existing_shipment = existing_result.scalar_one_or_none()
    if existing_shipment is not None:
        logger.info(
            "submit_shipment: duplicate detected — returning existing shipment_id=%s status=%s for user_id=%s",
            existing_shipment.id, existing_shipment.status, current_user.id,
        )
        # Return the existing shipment so the frontend shows its current state
        return ShipmentSubmitResponse(id=existing_shipment.id, status=existing_shipment.status)

    # If the user entered a container number in the BL field, move it to the right field
    container_number = payload.container_number
    bill_of_lading = payload.bill_of_lading
    if not container_number and _CONTAINER_RE.match(bill_of_lading):
        container_number = bill_of_lading
        bill_of_lading = None

    shipment = Shipment(
        container_number=container_number,
        bill_of_lading=bill_of_lading,
        carrier=payload.carrier,
        user_id=current_user.id,
        status="tracking_started",
    )
    db.add(shipment)
    await db.commit()
    await db.refresh(shipment)
    logger.info(
        "submit_shipment: created shipment_id=%s container=%s bl=%s carrier=%s status=%s for user_id=%s",
        shipment.id, shipment.container_number, shipment.bill_of_lading,
        shipment.carrier, shipment.status, current_user.id,
    )
    background_tasks.add_task(initial_track_shipment, shipment.id)
    return ShipmentSubmitResponse(id=shipment.id, status=shipment.status)


@router.post("/shipments/{shipment_id}/notify-me", status_code=status.HTTP_200_OK)
async def notify_me_when_ready(
    shipment_id: uuid.UUID,
    payload: NotifyMeRequest,
    db: DbSessionDep,
    current_user: CurrentUserDep,
) -> dict:
    """
    Save an email address to notify when tracking data becomes available for this shipment.
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

    shipment.notify_email = payload.email.strip()
    db.add(shipment)
    await db.commit()
    logger.info(
        "notify_me: user_id=%s shipment_id=%s notify_email=%s",
        current_user.id, shipment_id, shipment.notify_email,
    )
    return {"status": "saved"}


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
    logger.info(
        "approve_shipment: user_id=%s email=%s shipment_id=%s container=%s bl=%s — tracking started",
        current_user.id, current_user.email, shipment.id,
        shipment.container_number, shipment.bill_of_lading,
    )
    background_tasks.add_task(initial_track_shipment, shipment.id)

    return ShipmentRead.model_validate(shipment)


_ALLOWED_MIME_TYPES = {"application/pdf", "image/jpeg", "image/png", "image/webp"}


@router.post("/shipments/ocr")
async def ocr_bill_of_lading(
    current_user: CurrentUserDep,
    file: UploadFile = File(...),
) -> dict:
    """
    Upload a Bill of Lading document (PDF or image) and extract shipment data.
    Returns extracted fields for the frontend to pre-fill the submit form.
    The user must confirm and call /shipments/submit to actually create the shipment.
    """
    mime_type = file.content_type or ""
    if mime_type not in _ALLOWED_MIME_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported file type. Allowed: PDF, JPG, PNG, WEBP.",
        )

    file_bytes = await file.read()
    if len(file_bytes) > 10 * 1024 * 1024:  # 10 MB limit
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="File too large. Maximum size is 10 MB.",
        )

    logger.info(
        "ocr_bill_of_lading: user_id=%s email=%s filename=%s mime=%s size=%d",
        current_user.id, current_user.email, file.filename, mime_type, len(file_bytes),
    )
    result = await extract_bl_from_file(file_bytes, mime_type)
    if result is None:
        logger.warning("ocr_bill_of_lading: extraction failed for user_id=%s filename=%s", current_user.id, file.filename)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Could not extract data from the document. Please enter the details manually.",
        )

    logger.info(
        "ocr_bill_of_lading: extracted for user_id=%s — bl=%s container=%s carrier=%s",
        current_user.id, result.get("bill_of_lading"), result.get("container_number"), result.get("carrier"),
    )
    return result
