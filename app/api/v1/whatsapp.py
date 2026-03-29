"""
WhatsApp inbound webhook.

Inbound flow (Meta → Proxy → This server):
  The proxy forwards the raw Meta webhook payload to POST /api/v1/whatsapp/webhook
  with an added header:  X-Webhook-Secret: {WHATSAPP_WEBHOOK_SECRET}

  We run the logistics agent and return a synchronous reply:
  { "to": "<phone>", "message": { "type": "text", "content": "<reply>" } }

  If "to" / "message" are omitted the proxy treats the response as "no reply".

Outbound flow (async push) is handled by the notification service via
POST {WHATSAPP_PROXY_BASE_URL}/whatsapp/external/send with the same secret.
"""

import base64
import io
import logging
import re
import time
import uuid as _uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.logistics import run_agent
from app.core.config import settings
from app.db.session import get_db
from app.models.orm import Shipment, User, UserWhatsAppPhone

logger = logging.getLogger(__name__)

# Strip @mentions (e.g. "@15550001234 ") from group message bodies
_MENTION_RE = re.compile(r"@\d+\s*")


async def _create_shipments_from_data(
    containers: list[str],
    bls: list[str],
    carrier: str | None,
    user: User,
    db: AsyncSession,
) -> tuple[list[str], list[str]]:
    """
    Persist new shipments for any container/BL numbers not already tracked by
    this user. Returns (containers, bls) unchanged (for reply building).
    """
    if not containers and not bls:
        return [], []

    filters = []
    if containers:
        filters.append(Shipment.container_number.in_(containers))
    if bls:
        filters.append(Shipment.bill_of_lading.in_(bls))

    existing = (await db.execute(
        select(Shipment).where(Shipment.user_id == user.id, or_(*filters))
    )).scalars().all()

    existing_containers = {s.container_number for s in existing if s.container_number}
    existing_bls = {s.bill_of_lading for s in existing if s.bill_of_lading}

    new_count = 0
    for container in containers:
        if container not in existing_containers:
            bl = bls[0] if bls else None
            db.add(Shipment(container_number=container, bill_of_lading=bl, carrier=carrier, user_id=user.id, status="pending_approval"))
            logger.info("whatsapp: created shipment (pending_approval) container=%s bl=%s carrier=%s for user_id=%s", container, bl, carrier, user.id)
            new_count += 1

    for bl in bls:
        if bl not in existing_bls and not containers:
            db.add(Shipment(container_number=None, bill_of_lading=bl, carrier=carrier, user_id=user.id, status="pending_approval"))
            logger.info("whatsapp: created shipment (pending_approval) bl=%s carrier=%s for user_id=%s", bl, carrier, user.id)
            new_count += 1

    if existing_containers or existing_bls:
        logger.info("whatsapp: skipped %d already-tracked items for user_id=%s", len(existing_containers) + len(existing_bls), user.id)

    await db.commit()
    logger.info("whatsapp: _create_shipments_from_data done — new=%d containers=%s bls=%s user_id=%s", new_count, containers, bls, user.id)
    return containers, bls


async def _extract_and_create_shipments(
    text: str, user: User, db: AsyncSession
) -> tuple[list[str], list[str]]:
    """
    Use AI to extract container and BL numbers from *text*, create any new
    shipments, and return (container_numbers, bl_numbers).
    """
    from app.services.ai import extract_email_shipment_data

    result = await extract_email_shipment_data(subject="", body=text)
    if not result:
        logger.info("whatsapp: AI extraction found nothing in text (len=%d) for user_id=%s", len(text), user.id)
        return [], []

    containers = [c.upper() for c in (result.get("container_numbers") or [])]
    bls = [b.upper() for b in (result.get("bl_numbers") or [])]
    carrier = result.get("carrier") or None
    logger.info(
        "whatsapp: AI extracted containers=%s bls=%s carrier=%s from text (len=%d) for user_id=%s",
        containers, bls, carrier, len(text), user.id,
    )
    return await _create_shipments_from_data(containers, bls, carrier, user, db)


def _extract_text_from_pdf(data_b64: str) -> str | None:
    """
    Decode a base64-encoded PDF and extract its text layer using pdfplumber.
    Returns None if extraction fails or produces no usable text.
    """
    try:
        import pdfplumber
        pdf_bytes = base64.b64decode(data_b64)
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages).strip()
        return text or None
    except Exception as exc:
        logger.warning("PDF text extraction failed: %s", exc)
        return None


async def _handle_media_message(
    msg: "WhatsAppMessage",
    user: User,
    db: AsyncSession,
) -> str:
    """
    Handle a WhatsApp image or document message containing a bill of lading
    or other shipping document.

    - Image: GPT-4o vision extracts container/BL numbers directly from the photo.
    - Document (PDF): text layer extracted with pdfplumber, then GPT-4o NLP.
    - Caption-only fallback when no binary data is present.

    Returns a reply string ready to send back to the user.
    """
    from app.services.ai import extract_email_shipment_data, extract_image_shipment_data

    ai_result: dict | None = None

    if msg.type == "image" and msg.image is not None:
        img = msg.image
        if img.data:
            ai_result = await extract_image_shipment_data(
                base64_image=img.data,
                mime_type=img.mime_type or "image/jpeg",
                caption=img.caption,
            )
        if ai_result is None and img.caption:
            # No image data or vision failed — fall back to caption text
            ai_result = await extract_email_shipment_data(subject="", body=img.caption)

    elif msg.type == "document" and msg.document is not None:
        doc = msg.document
        extracted_text: str | None = None
        if doc.data and (doc.mime_type == "application/pdf" or (doc.filename or "").lower().endswith(".pdf")):
            extracted_text = _extract_text_from_pdf(doc.data)
        if extracted_text:
            ai_result = await extract_email_shipment_data(subject=doc.filename or "", body=extracted_text)
        elif doc.caption:
            ai_result = await extract_email_shipment_data(subject=doc.filename or "", body=doc.caption)

    _no_data_reply = (
        "Hi, TASA here. I received your file but couldn't find any container or BL numbers in it. "
        "Please ensure the document contains a container number or Bill of Lading reference."
    )

    if not ai_result:
        return _no_data_reply

    containers = [c.upper() for c in (ai_result.get("container_numbers") or [])]
    bls = [b.upper() for b in (ai_result.get("bl_numbers") or [])]
    carrier = ai_result.get("carrier") or None
    logger.info(
        "whatsapp: media message extracted containers=%s bls=%s carrier=%s type=%s for user_id=%s",
        containers, bls, carrier, msg.type, user.id,
    )

    if not containers and not bls:
        return _no_data_reply

    containers, bls = await _create_shipments_from_data(containers, bls, carrier, user, db)

    lines = []
    if bls:
        lines.append("BL number(s): " + ", ".join(bls))
    if containers:
        lines.append("Container(s): " + ", ".join(containers))
    items = "\n• ".join(lines)
    return (
        f"Hi, TASA here. I've added the following shipment(s) to your account:\n• {items}\n\n"
        "Would you like to approve them so I can start tracking?"
    )

# ---------------------------------------------------------------------------
# Message ID deduplication — prevents double-processing when the proxy or
# Meta retries the same webhook delivery.
# ---------------------------------------------------------------------------

_seen_message_ids: dict[str, float] = {}  # wamid → timestamp
_DEDUP_TTL = 300  # seconds to remember a message id (5 min)


def _is_duplicate(message_id: str) -> bool:
    """Return True if this message was already processed recently."""
    now = time.monotonic()
    # Evict expired entries
    expired = [k for k, t in _seen_message_ids.items() if now - t > _DEDUP_TTL]
    for k in expired:
        del _seen_message_ids[k]
    if message_id in _seen_message_ids:
        return True
    _seen_message_ids[message_id] = now
    return False


# ---------------------------------------------------------------------------
# Auth dependency — dedicated webhook secret
# ---------------------------------------------------------------------------


async def require_webhook_secret(
    x_webhook_secret: str | None = Header(None, alias="X-Webhook-Secret"),
) -> None:
    """Validate the proxy's webhook secret header."""
    if not settings.whatsapp_webhook_secret:
        return  # no secret configured — skip validation
    if x_webhook_secret != settings.whatsapp_webhook_secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing webhook secret",
        )


# ---------------------------------------------------------------------------
# Pydantic models — inbound (Meta WhatsApp payload forwarded by proxy)
# ---------------------------------------------------------------------------


class WhatsAppTextBody(BaseModel):
    body: str


class WhatsAppImageBody(BaseModel):
    caption: str | None = None
    mime_type: str | None = None
    sha256: str | None = None
    id: str | None = None
    data: str | None = None  # base64-encoded binary injected by the proxy


class WhatsAppDocumentBody(BaseModel):
    filename: str | None = None
    caption: str | None = None
    mime_type: str | None = None
    sha256: str | None = None
    id: str | None = None
    data: str | None = None  # base64-encoded binary injected by the proxy


class WhatsAppMessageContext(BaseModel):
    """Present when a message is sent in a group, as a reply, or forwarded."""
    group_id: str | None = None
    id: str | None = None  # quoted message id
    forwarded: bool = False
    frequently_forwarded: bool = False


class WhatsAppMessage(BaseModel):
    from_: str = Field(alias="from")
    id: str
    timestamp: str
    type: str
    text: WhatsAppTextBody | None = None
    image: WhatsAppImageBody | None = None
    document: WhatsAppDocumentBody | None = None
    context: WhatsAppMessageContext | None = None  # set for group messages


class WhatsAppMetadata(BaseModel):
    display_phone_number: str
    phone_number_id: str


class WhatsAppChangeValue(BaseModel):
    messaging_product: str
    metadata: WhatsAppMetadata
    messages: list[WhatsAppMessage] | None = None


class WhatsAppChange(BaseModel):
    value: WhatsAppChangeValue
    field: str


class WhatsAppEntry(BaseModel):
    id: str
    changes: list[WhatsAppChange]


class WhatsAppWebhookPayload(BaseModel):
    object: str
    entry: list[WhatsAppEntry]


# ---------------------------------------------------------------------------
# Pydantic models — outbound (reply format expected by proxy)
# ---------------------------------------------------------------------------


class WhatsAppReplyContent(BaseModel):
    type: str = "text"
    content: str | None = None
    template_name: str | None = None
    language: str | None = None
    components: list | None = None


class WhatsAppWebhookResponse(BaseModel):
    to: str
    message: WhatsAppReplyContent


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(
    prefix="/whatsapp",
    tags=["whatsapp"],
    dependencies=[Depends(require_webhook_secret)],
)

DbSessionDep = Annotated[AsyncSession, Depends(get_db)]

FALLBACK_MESSAGE = "Hi, TASA here. I'm unable to process your request right now — please try again in a moment."
TEXT_ONLY_MESSAGE = "Hi, TASA here. I can only process text messages at the moment. Please send a text message."
UNREGISTERED_MESSAGE = (
    "Your phone number is not registered with Tydline. "
    "Please sign up first at tydline.com to start tracking shipments via WhatsApp."
)


def _normalize_phone(raw: str) -> str:
    """Strip leading '+' so phone format matches Meta's style (e.g. 233XXXXXXXXX)."""
    return raw.lstrip("+")


def _make_reply(to: str, content: str) -> WhatsAppWebhookResponse:
    return WhatsAppWebhookResponse(
        to=to,
        message=WhatsAppReplyContent(type="text", content=content),
    )


def _make_template_reply(to: str, template_name: str, language: str = "en", components: list | None = None) -> WhatsAppWebhookResponse:
    return WhatsAppWebhookResponse(
        to=to,
        message=WhatsAppReplyContent(
            type="template",
            template_name=template_name,
            language=language,
            components=components or [],
        ),
    )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/webhook", response_model=WhatsAppWebhookResponse)
async def whatsapp_webhook(
    payload: WhatsAppWebhookPayload,
    db: DbSessionDep,
) -> WhatsAppWebhookResponse:
    """Receive a forwarded WhatsApp message, run the agent, return the reply."""

    try:
        # --- Extract the first message ----------------------------------------
        if not payload.entry or not payload.entry[0].changes:
            # Empty payload — acknowledge silently
            return _make_reply("unknown", "")

        first_change = payload.entry[0].changes[0]
        messages = first_change.value.messages

        if not messages:
            # Status update — acknowledge silently
            sender = first_change.value.metadata.display_phone_number
            return _make_reply(_normalize_phone(sender), "")

        msg = messages[0]
        sender_phone = _normalize_phone(msg.from_)

        # --- Deduplication -----------------------------------------------------
        if _is_duplicate(msg.id):
            logger.info("Duplicate webhook for message %s — skipping", msg.id)
            return _make_reply(sender_phone, "")

        # --- Image / document messages (bill of lading photo, PDF, etc.) -------
        if msg.type in ("image", "document"):
            wp_result = await db.execute(
                select(UserWhatsAppPhone).where(UserWhatsAppPhone.phone == sender_phone)
            )
            wp_entry = wp_result.scalar_one_or_none()
            media_user: User | None = None
            if wp_entry:
                user_result = await db.execute(select(User).where(User.id == wp_entry.user_id))
                media_user = user_result.scalar_one_or_none()
            if media_user is None:
                logger.info("Unregistered phone %s — rejecting media message", sender_phone[-4:])
                return _make_reply(sender_phone, UNREGISTERED_MESSAGE)
            logger.info("Media message (type=%s) from user %s (phone ...%s)", msg.type, media_user.id, sender_phone[-4:])
            reply = await _handle_media_message(msg, media_user, db)
            return _make_reply(sender_phone, reply)

        # --- Unsupported message types (audio, video, sticker, reaction, …) ---
        if msg.type != "text" or msg.text is None:
            logger.info("Non-text message (type=%s) from %s — skipping", msg.type, sender_phone[-4:])
            return _make_reply(sender_phone, TEXT_ONLY_MESSAGE)

        # Strip @mentions so group messages work cleanly with the agent
        message_text = _MENTION_RE.sub("", msg.text.body).strip()
        if not message_text:
            return _make_reply(sender_phone, TEXT_ONLY_MESSAGE)

        is_group = msg.context is not None and msg.context.group_id is not None
        if is_group:
            logger.info("Group message from ...%s (group %s)", sender_phone[-4:], msg.context.group_id)

        # --- User lookup by phone ----------------------------------------------
        wp_result = await db.execute(
            select(UserWhatsAppPhone).where(UserWhatsAppPhone.phone == sender_phone)
        )
        wp_entry = wp_result.scalar_one_or_none()
        user: User | None = None
        if wp_entry:
            user_result = await db.execute(select(User).where(User.id == wp_entry.user_id))
            user = user_result.scalar_one_or_none()

        if user is None:
            logger.info("Unregistered phone %s — rejecting", sender_phone[-4:])
            return _make_reply(sender_phone, UNREGISTERED_MESSAGE)

        is_forwarded = msg.context is not None and msg.context.forwarded

        # --- Forwarded message: extract shipping data, skip agent --------------
        if is_forwarded:
            logger.info(
                "whatsapp: forwarded message from user_id=%s phone=...%s text_len=%d — extracting shipping data",
                user.id, sender_phone[-4:], len(message_text),
            )
            containers, bls = await _extract_and_create_shipments(message_text, user, db)
            if containers or bls:
                lines = []
                if bls:
                    lines.append("BL number(s): " + ", ".join(bls))
                if containers:
                    lines.append("Container(s): " + ", ".join(containers))
                items = "\n• ".join(lines)
                reply = f"Hi, TASA here. I've added the following shipment(s) to your account:\n• {items}\n\nWould you like to approve them so I can start tracking?"
            else:
                reply = "Hi, TASA here. I received the forwarded message but couldn't find any container or BL numbers in it. Please forward a message that includes a BL or container number."
            return _make_reply(sender_phone, reply)

        # --- Hello greeting: trigger onboarding flow ---------------------------
        if message_text.strip().lower() == "hello":
            logger.info("whatsapp: hello greeting from user_id=%s — sending onboarding_form template", user.id)
            return _make_template_reply(sender_phone, "onboarding_form")

        # --- Direct message: extract any shipping refs then run agent ----------
        logger.info(
            "whatsapp: text message from user_id=%s phone=...%s is_group=%s is_forwarded=%s text_len=%d body=%r",
            user.id, sender_phone[-4:], is_group, is_forwarded, len(message_text), message_text[:120],
        )
        containers, bls = await _extract_and_create_shipments(message_text, user, db)
        logger.info(
            "whatsapp: extraction result — containers=%s bls=%s user_id=%s",
            containers, bls, user.id,
        )

        agent_message = message_text
        if containers or bls:
            parts = []
            if bls:
                parts.append("BL numbers: " + ", ".join(bls))
            if containers:
                parts.append("container numbers: " + ", ".join(containers))
            agent_message = (
                f"{message_text}\n\n"
                f"[EXTRACTED: {'; '.join(parts)}. "
                f"Shipments have been added and are pending approval. "
                f"Tell the user the shipment has been added and ask if they would like to approve it to begin tracking. Do not ask for container numbers.]"
            )
            logger.info("whatsapp: injected EXTRACTED tag into agent message for user_id=%s", user.id)

        # --- Run the agent -----------------------------------------------------
        logger.info(
            "whatsapp: running agent for user_id=%s agent_message_len=%d",
            user.id, len(agent_message),
        )
        reply = await run_agent(str(user.id), agent_message, db)
        logger.info(
            "whatsapp: agent reply for user_id=%s — reply_len=%s reply_is_none=%s",
            user.id, len(reply) if reply else 0, reply is None,
        )

        if reply is None:
            logger.warning("whatsapp: agent returned None for user_id=%s — sending fallback", user.id)
            return _make_reply(sender_phone, FALLBACK_MESSAGE)

        return _make_reply(sender_phone, reply)

    except Exception:
        logger.exception("Unexpected error processing WhatsApp webhook")
        return _make_reply("unknown", FALLBACK_MESSAGE)
