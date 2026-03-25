"""
Outbound email service — provider-agnostic.

Set EMAIL_PROVIDER=resend  (+ RESEND_API_KEY)  to use Resend.
Set EMAIL_PROVIDER=postmark (default)           to use Postmark.

Inbound email is always handled by Postmark via /api/v1/email/inbound — this
service is for outbound only (magic links, shipment alerts, etc.).
"""

import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


async def send_email(
    to: str,
    subject: str,
    text_body: str,
    html_body: str | None = None,
) -> None:
    """
    Send an outbound email via the configured provider.
    Logs a warning on failure but never raises — callers should not crash on email errors.
    """
    if not settings.email_from:
        logger.warning("EMAIL_FROM not set — skipping outbound email to %s", to)
        return

    provider = (settings.email_provider or "postmark").lower()

    if provider == "resend":
        await _send_via_resend(to=to, subject=subject, text_body=text_body, html_body=html_body)
    else:
        await _send_via_postmark(to=to, subject=subject, text_body=text_body, html_body=html_body)


async def _send_via_postmark(
    to: str,
    subject: str,
    text_body: str,
    html_body: str | None = None,
) -> None:
    if not settings.postmark_server_token:
        logger.warning("POSTMARK_SERVER_TOKEN not set — skipping email to %s", to)
        return

    from_display = f"{settings.postmark_from_name} <{settings.email_from}>"
    payload: dict = {
        "From": from_display,
        "To": to,
        "Subject": subject,
        "TextBody": text_body,
    }
    if html_body:
        payload["HtmlBody"] = html_body

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.postmarkapp.com/email",
                headers={
                    "X-Postmark-Server-Token": settings.postmark_server_token,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        if not resp.is_success:
            logger.warning("Postmark outbound failed (%s): %s", resp.status_code, resp.text)
    except Exception:
        logger.warning("Postmark outbound error sending to %s", to, exc_info=True)


async def _send_via_resend(
    to: str,
    subject: str,
    text_body: str,
    html_body: str | None = None,
) -> None:
    if not settings.resend_api_key:
        logger.warning("RESEND_API_KEY not set — skipping email to %s", to)
        return

    from_display = f"{settings.postmark_from_name} <{settings.email_from}>"
    payload: dict = {
        "from": from_display,
        "to": [to],
        "subject": subject,
        "text": text_body,
    }
    if html_body:
        payload["html"] = html_body

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {settings.resend_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        if not resp.is_success:
            logger.warning("Resend outbound failed (%s): %s", resp.status_code, resp.text)
    except Exception:
        logger.warning("Resend outbound error sending to %s", to, exc_info=True)
