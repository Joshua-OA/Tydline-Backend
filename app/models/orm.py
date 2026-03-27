import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    company_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tracking_email: Mapped[str | None] = mapped_column(String(255), unique=True, index=True, nullable=True)
    subscription_status: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    magic_link_token: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    otp_code: Mapped[str | None] = mapped_column(String(255), nullable=True)
    otp_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    auth_token: Mapped[str | None] = mapped_column(String(255), unique=True, index=True, nullable=True)
    plan: Mapped[str | None] = mapped_column(String(32), nullable=True)             # starter | growth | pro | custom
    payment_session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payment_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payment_pending_plan: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    shipments: Mapped[list["Shipment"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    notify_parties: Mapped[list["NotifyParty"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    whatsapp_phones: Mapped[list["UserWhatsAppPhone"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    authorized_emails: Mapped[list["UserAuthorizedEmail"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Shipment(Base):
    __tablename__ = "shipments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    container_number: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    bill_of_lading: Mapped[str | None] = mapped_column(String(64), nullable=True)
    carrier: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="created")
    eta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    predicted_eta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    vessel: Mapped[str | None] = mapped_column(String(128), nullable=True)
    origin: Mapped[str | None] = mapped_column(String(128), nullable=True)
    destination: Mapped[str | None] = mapped_column(String(128), nullable=True)
    demurrage_risk: Mapped[str | None] = mapped_column(String(16), nullable=True)
    free_days_remaining: Mapped[int | None] = mapped_column(nullable=True)
    timeline_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    notify_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    shipsgo_shipment_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="shipments")
    notifications: Mapped[list["Notification"]] = relationship(
        back_populates="shipment", cascade="all, delete-orphan"
    )
    events: Mapped[list["ShipmentEvent"]] = relationship(
        back_populates="shipment", cascade="all, delete-orphan"
    )


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    shipment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shipments.id", ondelete="CASCADE"), nullable=False
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    shipment: Mapped["Shipment"] = relationship(back_populates="notifications")


class ShipmentEvent(Base):
    __tablename__ = "shipment_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    shipment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shipments.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    location: Mapped[str | None] = mapped_column(String(128), nullable=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    shipment: Mapped["Shipment"] = relationship(back_populates="events")


class RiskAlert(Base):
    __tablename__ = "risk_alerts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    shipment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shipments.id", ondelete="CASCADE"), nullable=False
    )
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AIGeneratedMessage(Base):
    __tablename__ = "ai_generated_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    shipment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shipments.id", ondelete="CASCADE"), nullable=True
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class NotifyParty(Base):
    """
    A contact that should receive shipment notifications on behalf of the company.

    channel: "email" | "whatsapp"
    contact_value: email address or phone number (e.g. "233XXXXXXXXX")
    WhatsApp notify parties require an active subscription.
    """

    __tablename__ = "notify_parties"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)   # "email" | "whatsapp"
    contact_value: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="notify_parties")


class InboundEmail(Base):
    """
    Stores every inbound email received via the Postmark webhook.

    User matching: from_email is looked up against users.email.
    Container extraction: ISO 6346 container numbers found in subject + body.
    Shipment linking: matched_shipment_ids holds UUIDs of shipments belonging
    to the matched user whose container_number appears in the email.
    mem0_stored: True once the email content has been fed into Mem0 for the agent.
    """

    __tablename__ = "inbound_emails"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Matched user — nullable when sender is not a registered user
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    from_email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    from_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    to_email: Mapped[str] = mapped_column(String(512), nullable=False)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Postmark message ID — unique to prevent duplicate processing
    message_id: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True, index=True)
    # ISO 6346 container numbers extracted by AI (e.g. ["MSCU1234567"])
    container_numbers: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Bill of Lading / booking reference numbers extracted by AI
    bl_numbers: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Shipping carrier/line identified by AI (e.g. "Maersk")
    carrier: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # One-sentence AI summary of the email
    email_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # UUIDs (as strings) of shipments in the DB that match containers or BLs found
    matched_shipment_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Whether this email's context has been stored in Mem0
    mem0_stored: Mapped[bool] = mapped_column(nullable=False, default=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped["User | None"] = relationship()


class UserWhatsAppPhone(Base):
    """
    Maps WhatsApp phone numbers to user accounts.
    One user can have multiple numbers (e.g. personal + work).
    Each phone number can only belong to one user.
    """

    __tablename__ = "user_whatsapp_phones"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    phone: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="whatsapp_phones")


class UserAuthorizedEmail(Base):
    """
    Maps authorized sender email addresses to user accounts.
    Inbound emails from these addresses are attributed to the linked user.
    Each email address can only belong to one user.
    """

    __tablename__ = "user_authorized_emails"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="authorized_emails")


class Coupon(Base):
    """
    A coupon code that activates a plan without payment.

    plan: which plan the coupon grants (starter | growth | pro | custom)
    max_uses: None means unlimited
    uses_count: incremented each time the coupon is successfully redeemed
    is_active: admin can deactivate a coupon without deleting it
    expires_at: None means never expires
    """

    __tablename__ = "coupons"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    plan: Mapped[str] = mapped_column(String(32), nullable=False)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)
    max_uses: Mapped[int | None] = mapped_column(nullable=True)
    uses_count: Mapped[int] = mapped_column(nullable=False, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
