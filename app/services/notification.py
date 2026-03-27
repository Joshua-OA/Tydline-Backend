"""
Notification service responsible for:
- Persisting notification records
- Dispatching outbound messages (email, WhatsApp, SMS) via pluggable channels
- AI-generated alerts via Groq (Phase 5)
"""

import logging
import math
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import orm
from app.services.ai import draft_logistics_alert
from app.utils.retry import with_retries  # still used by _send_whatsapp / _send_sms

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).parent.parent.parent / "emails"
_DASHBOARD_URL = "https://tydline.com/dashboard"


def _render_shipment_update_html(
    container_number: str,
    old_status: str,
    new_status: str,
    message: str,
) -> str | None:
    template_path = _TEMPLATE_DIR / "shipment-update.html"
    try:
        html = template_path.read_text(encoding="utf-8")
        html = html.replace("{{container_number}}", container_number)
        html = html.replace("{{old_status}}", old_status)
        html = html.replace("{{new_status}}", new_status)
        html = html.replace("{{message}}", message.replace("\n", "<br>"))
        html = html.replace("{{dashboard_url}}", _DASHBOARD_URL)
        return html
    except Exception as exc:
        logger.warning("Could not load shipment-update email template: %s", exc)
        return None


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


def _relative_arrival(eta: datetime | None) -> str:
    """Return a human-readable relative time string like '2 weeks' or '3 days'."""
    if eta is None:
        return "soon"
    now = datetime.now(timezone.utc)
    eta_aware = eta if eta.tzinfo else eta.replace(tzinfo=timezone.utc)
    delta_days = (eta_aware - now).days
    if delta_days <= 0:
        return "today"
    if delta_days == 1:
        return "tomorrow"
    if delta_days < 7:
        return f"{delta_days} days"
    weeks = math.ceil(delta_days / 7)
    return f"{weeks} week{'s' if weeks != 1 else ''}"


async def _send_whatsapp_template(
    phone_number: str,
    bl_number: str,
    eta_date: str,
    relative_arrival: str,
) -> None:
    """
    Send the approved 'shipment_update' WhatsApp template via the proxy.

    Template body: "Hello, your shipment with Bill of Lading Number {{1}} will arrive
    on the {{2}} which is arriving in {{3}}. This is the perfect time to begin all
    your clearing processes"
    """
    if not (settings.whatsapp_proxy_url and settings.whatsapp_webhook_secret):
        logger.debug("WhatsApp proxy not configured — skipping template message")
        return

    phone = phone_number.lstrip("+")
    phone_suffix = phone[-4:] if len(phone) >= 4 else "****"

    payload: dict[str, Any] = {
        "to": phone,
        "message": {
            "type": "template",
            "template_name": "shipment_update",
            "language": "en_US",
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": bl_number},
                        {"type": "text", "text": eta_date},
                        {"type": "text", "text": relative_arrival},
                    ],
                }
            ],
        },
    }

    async def _post() -> httpx.Response:
        async with httpx.AsyncClient(timeout=10.0) as client:
            return await client.post(
                settings.whatsapp_proxy_url,
                headers={
                    "X-Webhook-Secret": settings.whatsapp_webhook_secret,
                    "Content-Type": "application/json",
                },
                json=payload,
            )

    resp = await with_retries(_post)
    if resp is None or not resp.is_success:
        logger.warning(
            "WhatsApp template push failed or retries exhausted (to ...%s)", phone_suffix
        )


async def send_approval_tracking_notification(
    session: AsyncSession,
    shipment: orm.Shipment,
) -> None:
    """
    Called once when a shipment transitions from 'tracking_started' and real
    tracking data arrives for the first time.

    Notifies all available channels:
    - Email: user + email notify_parties — AI-drafted body via shipment-update.html
    - WhatsApp: user's registered WA phones + WhatsApp notify_parties — shipment_update template
    """
    user = shipment.user

    # ── Gather notify parties ────────────────────────────────────────────────
    result = await session.execute(
        select(orm.NotifyParty).where(orm.NotifyParty.user_id == user.id)
    )
    notify_parties: list[orm.NotifyParty] = list(result.scalars().all())

    result = await session.execute(
        select(orm.UserWhatsAppPhone).where(orm.UserWhatsAppPhone.user_id == user.id)
    )
    wa_phones: list[orm.UserWhatsAppPhone] = list(result.scalars().all())

    # ── Build message bodies ─────────────────────────────────────────────────
    new_status = shipment.status or "In Transit"
    old_status = "tracking_started"

    context = _build_alert_context(shipment, new_status)
    ai_body = await draft_logistics_alert(context)

    fallback_lines = [
        f"Hi, TASA here. I'm now tracking your shipment {shipment.container_number or shipment.bill_of_lading}.",
        f"Status: {new_status}",
    ]
    if shipment.eta:
        fallback_lines.append(f"ETA: {shipment.eta.strftime('%d %B %Y')}")
    if shipment.vessel:
        fallback_lines.append(f"Vessel: {shipment.vessel}")
    fallback_body = "\n".join(fallback_lines)

    body = ai_body if ai_body else fallback_body

    # Persist notification record
    session.add(orm.Notification(shipment_id=shipment.id, message=body))
    if ai_body:
        session.add(orm.AIGeneratedMessage(shipment_id=shipment.id, channel="multi", message=body))
    await session.commit()

    # ── Email recipients ─────────────────────────────────────────────────────
    email_recipients: list[str] = []
    if user.email:
        email_recipients.append(user.email)
    email_recipients.extend(
        p.contact_value for p in notify_parties if p.channel == "email"
    )

    if email_recipients:
        html_body = _render_shipment_update_html(
            container_number=shipment.container_number or "",
            old_status=old_status,
            new_status=new_status,
            message=body,
        )
        from app.services.email import send_email
        for recipient in email_recipients:
            await send_email(
                to=recipient,
                subject=f"Shipment update: {shipment.container_number or shipment.bill_of_lading}",
                text_body=body,
                html_body=html_body,
            )

    # ── WhatsApp recipients ───────────────────────────────────────────────────
    bl_number = shipment.bill_of_lading or shipment.container_number or "N/A"
    eta_date = shipment.eta.strftime("%-d %B %Y") if shipment.eta else "TBD"
    relative = _relative_arrival(shipment.eta)

    wa_recipients: list[str] = [p.phone for p in wa_phones]
    wa_recipients.extend(
        p.contact_value for p in notify_parties if p.channel == "whatsapp"
    )

    for phone in wa_recipients:
        await _send_whatsapp_template(
            phone_number=phone,
            bl_number=bl_number,
            eta_date=eta_date,
            relative_arrival=relative,
        )


_APP_BASE_URL = "https://tydline.com"


async def send_approval_request_notification(shipment_id: uuid.UUID) -> None:
    """
    Background task: notify the shipment owner that their submission is pending approval.
    Sends an email with an Approve button and a WhatsApp text with the approval link.
    Uses its own DB session so it can run as a standalone background task.
    """
    from app.db.session import AsyncSessionLocal
    from app.services.email import send_email

    approve_url = f"{_APP_BASE_URL}/dashboard/approvals?approve={shipment_id}"

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(orm.Shipment).where(orm.Shipment.id == shipment_id)
        )
        shipment = result.scalar_one_or_none()
        if not shipment:
            logger.warning("send_approval_request_notification: shipment %s not found", shipment_id)
            return

        result = await session.execute(
            select(orm.User).where(orm.User.id == shipment.user_id)
        )
        user = result.scalar_one_or_none()
        if not user:
            return

        bl = shipment.bill_of_lading or shipment.container_number or "N/A"
        container = shipment.container_number or "N/A"
        carrier = shipment.carrier or "Not specified"

        # ── Email ────────────────────────────────────────────────────────────
        if user.email:
            html_body: str | None = None
            template_path = _TEMPLATE_DIR / "approval-request.html"
            try:
                html = template_path.read_text(encoding="utf-8")
                html = html.replace("{{bill_of_lading}}", bl)
                html = html.replace("{{container_number}}", container)
                html = html.replace("{{carrier}}", carrier)
                html = html.replace("{{approve_url}}", approve_url)
                html_body = html
            except Exception as exc:
                logger.warning("Could not load approval-request email template: %s", exc)

            text_body = (
                f"Hi, TASA here. I've received a new shipment and need your approval before I can start tracking it.\n\n"
                f"Bill of Lading: {bl}\n"
                f"Container: {container}\n"
                f"Carrier: {carrier}\n\n"
                f"Approve it here:\n{approve_url}\n\n"
                f"If you don't act within 3 days I'll approve it automatically.\n\n"
                f"— TASA"
            )
            await send_email(
                to=user.email,
                subject=f"Action required: approve shipment {bl}",
                text_body=text_body,
                html_body=html_body,
            )

        # ── WhatsApp ─────────────────────────────────────────────────────────
        wa_result = await session.execute(
            select(orm.UserWhatsAppPhone).where(orm.UserWhatsAppPhone.user_id == user.id)
        )
        wa_phones = list(wa_result.scalars().all())
        wa_text = (
            f"Hi, TASA here. I've received a new shipment (BL: {bl}) and need your approval before I can start tracking it.\n\n"
            f"Tap the link below to approve:\n{approve_url}"
        )
        for phone_record in wa_phones:
            await _send_whatsapp(phone_record.phone, wa_text)


async def send_tracking_not_found_notification(
    session: AsyncSession,
    shipment: orm.Shipment,
) -> None:
    """
    Notify the user when ShipsGo returns no data for their BL/container after approval.
    Sends via email and WhatsApp (text) so they know to verify the reference.
    """
    from app.services.email import send_email

    result = await session.execute(
        select(orm.User).where(orm.User.id == shipment.user_id)
    )
    user = result.scalar_one_or_none()
    if not user:
        return

    reference = shipment.bill_of_lading or shipment.container_number or "your shipment"
    subject = f"Tracking not found: {reference}"
    text_body = (
        f"Hi, TASA here. I tried to look up tracking information for {reference} "
        f"but couldn't find anything.\n\n"
        f"This usually means the number was entered incorrectly or the carrier "
        f"hasn't published data yet.\n\n"
        f"Please double-check the reference and update it in your dashboard, "
        f"or contact your carrier for the correct number.\n\n"
        f"— TASA"
    )

    html_body: str | None = None
    template_path = _TEMPLATE_DIR / "tracking-not-found.html"
    try:
        html = template_path.read_text(encoding="utf-8")
        html = html.replace("{{reference}}", reference)
        html = html.replace("{{dashboard_url}}", _DASHBOARD_URL)
        html_body = html
    except Exception as exc:
        logger.warning("Could not load tracking-not-found email template: %s", exc)

    if user.email:
        await send_email(to=user.email, subject=subject, text_body=text_body, html_body=html_body)

    result = await session.execute(
        select(orm.UserWhatsAppPhone).where(orm.UserWhatsAppPhone.user_id == user.id)
    )
    wa_phones = list(result.scalars().all())
    wa_text = (
        f"Hi, TASA here. I tried to find tracking info for {reference} but came up empty. "
        f"Please double-check the BL number on your Tydline dashboard or contact your carrier."
    )
    for phone_record in wa_phones:
        await _send_whatsapp(phone_record.phone, wa_text)


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
        html_body = _render_shipment_update_html(
            container_number=shipment.container_number or "",
            old_status=old_status,
            new_status=new_status,
            message=message,
        )
        from app.services.email import send_email
        await send_email(
            to=user.email,
            subject=f"Shipment update: {shipment.container_number}",
            text_body=message,
            html_body=html_body,
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
        html_body = _render_shipment_update_html(
            container_number=shipment.container_number or "",
            old_status=old_status,
            new_status=new_status,
            message=body,
        )
        from app.services.email import send_email
        await send_email(
            to=user.email,
            subject=subject,
            text_body=body,
            html_body=html_body,
        )
    if user.phone:
        await _send_whatsapp(phone_number=user.phone, message=body)
        await _send_sms(phone_number=user.phone, message=body)
