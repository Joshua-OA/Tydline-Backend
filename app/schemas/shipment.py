import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

# ISO 6346 container number: 4 letters (owner + equipment category) + 6 digits + 1 check digit
_CONTAINER_RE = re.compile(r"^[A-Z]{4}\d{7}$")


class UserBase(BaseModel):
    email: EmailStr
    phone: str | None = None


class UserRead(UserBase):
    id: uuid.UUID
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ShipmentCreate(BaseModel):
    container_number: str = Field(..., examples=["MSCU1234567"])
    bill_of_lading: str | None = Field(None, max_length=64)
    carrier: str | None = Field(None, max_length=64)
    user_id: uuid.UUID

    @field_validator("container_number")
    @classmethod
    def validate_container_number(cls, v: str) -> str:
        normalised = v.strip().upper()
        if not _CONTAINER_RE.match(normalised):
            raise ValueError(
                "container_number must be 4 letters followed by 7 digits (e.g. MSCU1234567)"
            )
        return normalised


class ShipmentRead(BaseModel):
    id: uuid.UUID
    container_number: str | None
    bill_of_lading: str | None
    carrier: str | None
    status: str
    eta: datetime | None
    predicted_eta: datetime | None
    demurrage_risk: str | None
    free_days_remaining: int | None
    last_updated: datetime
    user_id: uuid.UUID
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ShipmentListResponse(BaseModel):
    items: list[ShipmentRead]
    total: int


class TrackShipmentResponse(BaseModel):
    status: str
    container_number: str


class NotificationRead(BaseModel):
    id: uuid.UUID
    shipment_id: uuid.UUID
    message: str
    sent_at: datetime

    model_config = ConfigDict(from_attributes=True)
