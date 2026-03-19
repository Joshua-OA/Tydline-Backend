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

import logging
import re
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.logistics import run_agent
from app.core.config import settings
from app.db.session import get_db
from app.models.orm import User

# Strip @mentions (e.g. "@15550001234 ") from group message bodies
_MENTION_RE = re.compile(r"@\d+\s*")

logger = logging.getLogger(__name__)

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


class WhatsAppMessageContext(BaseModel):
    """Present when a message is sent in a group or as a reply."""
    group_id: str | None = None
    id: str | None = None  # quoted message id


class WhatsAppMessage(BaseModel):
    from_: str = Field(alias="from")
    id: str
    timestamp: str
    type: str
    text: WhatsAppTextBody | None = None
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
    content: str


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

FALLBACK_MESSAGE = "Sorry, I'm unable to process your request right now. Please try again later."
TEXT_ONLY_MESSAGE = "I can only process text messages at the moment. Please send a text message."
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
        message=WhatsAppReplyContent(content=content),
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

        # --- Non-text messages -------------------------------------------------
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
        result = await db.execute(select(User).where(User.phone == sender_phone))
        user = result.scalar_one_or_none()

        if user is None:
            logger.info("Unregistered phone %s — rejecting", sender_phone[-4:])
            return _make_reply(sender_phone, UNREGISTERED_MESSAGE)

        # --- Run the agent -----------------------------------------------------
        logger.info("WhatsApp message from user %s (phone ...%s)", user.id, sender_phone[-4:])
        reply = await run_agent(str(user.id), message_text, db)

        if reply is None:
            return _make_reply(sender_phone, FALLBACK_MESSAGE)

        return _make_reply(sender_phone, reply)

    except Exception:
        logger.exception("Unexpected error processing WhatsApp webhook")
        return _make_reply("unknown", FALLBACK_MESSAGE)
