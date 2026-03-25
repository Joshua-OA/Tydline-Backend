"""
Notification service responsible for:
- Persisting notification records
- Dispatching outbound messages (email, WhatsApp, SMS) via pluggable channels
- AI-generated alerts via Groq (Phase 5)
"""

import logging
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import orm
from app.services.ai import draft_logistics_alert
from app.utils.retry import with_retries  # still used by _send_whatsapp / _send_sms

logger = logging.getLogger(__name__)


async def _send_email(recipient_email: str, subject: str, body: str) -> None:
    from app.services.email import send_email
    await send_email(to=recipient_email, subject=subject, text_body=body)


async def _send_whatsapp(phone_number: str, message: str) -> None:
    """
    Push a text message via the external WhatsApp proxy (async push path).

    Proxy endpoint: POST {WHATSAPP_PROXY_BASE_URL}/whatsapp/external/send
    Auth header:    X-Webhook-Secret: {WHATSAPP_WEBHOOK_SECRET}

    Payload:
        {
            "to": "<phone without +>",
            "message": { "type": "text", "content": "<body>" }
        }
    """
    if not (settings.whatsapp_proxy_url and settings.whatsapp_webhook_secret):
        logger.debug("WhatsApp proxy not configured — skipping outbound message")
        return

    send_url = settings.whatsapp_proxy_url
    phone = phone_number.lstrip("+")
    phone_suffix = phone[-4:] if len(phone) >= 4 else "****"

    payload: dict[str, Any] = {
        "to": phone,
        "message": {
            "type": "text",
            "content": message[:4096],
        },
    }

    async def _post() -> httpx.Response:
        async with httpx.AsyncClient(timeout=10.0) as client:
            return await client.post(
                send_url,
                headers={
                    "X-Webhook-Secret": settings.whatsapp_webhook_secret,
                    "Content-Type": "application/json",
                },
                json=payload,
            )

    resp = await with_retries(_post)
    if resp is None or not resp.is_success:
        logger.warning("WhatsApp proxy push failed or retries exhausted (to ...%s)", phone_suffix)


async def _send_sms(phone_number: str, message: str) -> None:
    """
    Placeholder SMS sender. Replace with Twilio or other SMS provider.
    Set SMS_API_KEY and implement the provider call below to enable.
    """
    if not settings.sms_api_key or settings.sms_api_key.startswith("your-"):
        return

    # TODO: implement SMS provider (e.g. Twilio) — currently a no-op stub.
    logger.info("SMS skipped for %s — provider not yet implemented.", phone_number[-4:])


async def send_shipment_update_notification(
    session: AsyncSession,
    shipment: orm.Shipment,
    old_status: str,
    new_status: str,
) -> None:
    """
    High-level notification used when shipment status changes.
    Persists Notification record and dispatches via available channels.
    """
    user = shipment.user
    message = f"Container {shipment.container_number} status changed from {old_status} to {new_status}."
    if new_status.lower() == "arrived at port":
        message += " Recommended action: Begin customs clearance to avoid demurrage fees."

    notification = orm.Notification(
        shipment_id=shipment.id,
        message=message,
    )
    session.add(notification)
    await session.commit()

    # Dispatch through available channels.
    if user.email:
        await _send_email(
            recipient_email=user.email,
            subject=f"Shipment update: {shipment.container_number}",
            body=message,
        )

    if user.phone:
        # Future: differentiate between WhatsApp and SMS per user preferences.
        await _send_whatsapp(phone_number=user.phone, message=message)
        await _send_sms(phone_number=user.phone, message=message)


def _build_alert_context(shipment: orm.Shipment, new_status: str) -> dict[str, Any]:
    """Build context dict for AI alert generator."""
    return {
        "container_number": shipment.container_number,
        "status": new_status,
        "location": getattr(shipment, "location", None) or getattr(shipment, "carrier", None),
        "eta": shipment.eta.isoformat() if shipment.eta else None,
        "free_days_remaining": getattr(shipment, "free_days_remaining", None),
        "risk_level": getattr(shipment, "demurrage_risk", None),
    }


async def send_shipment_status_change_notification(
    session: AsyncSession,
    shipment: orm.Shipment,
    old_status: str,
    new_status: str,
) -> None:
    """
    Status-change-driven alerts. Uses Groq for AI-generated message when configured;
    otherwise falls back to template. Persists to notifications and optionally
    to ai_generated_messages; sends via email and WhatsApp.
    """
    user = shipment.user

    # Fallback template
    message_lines = [
        f"Container {shipment.container_number} has changed status.",
        f"Previous status: {old_status}",
        f"New status: {new_status}",
    ]
    if new_status.lower() == "arrived at port":
        message_lines.append("Free days countdown has started.")
        message_lines.append("Recommended action: Begin customs clearance.")
    fallback_body = "\n".join(message_lines)

    # AI-generated body when Groq is configured
    context = _build_alert_context(shipment, new_status)
    ai_body = await draft_logistics_alert(context)
    body = ai_body if ai_body else fallback_body

    notification = orm.Notification(
        shipment_id=shipment.id,
        message=body,
    )
    session.add(notification)
    await session.commit()

    if ai_body:
        session.add(
            orm.AIGeneratedMessage(
                shipment_id=shipment.id,
                channel="multi",
                message=body,
            )
        )
        await session.commit()

    subject = "Shipment Update"
    if user.email:
        await _send_email(recipient_email=user.email, subject=subject, body=body)
    if user.phone:
        await _send_whatsapp(phone_number=user.phone, message=body)
        await _send_sms(phone_number=user.phone, message=body)
