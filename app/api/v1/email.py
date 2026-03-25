"""
Inbound email webhook — supports Resend and Postmark.

Setup with Resend:
  1. Verify your inbound domain (e.g. track.tydline.com) in Resend → Domains.
  2. Set the inbound webhook URL to: https://your-api.tydline.com/api/v1/email/inbound
  3. Set EMAIL_PROVIDER=resend and RESEND_API_KEY in .env.
  4. Set RESEND_WEBHOOK_SECRET=whsec_... in .env (from Resend → Webhooks).
  5. Importers forward or CC their shipping emails to: anything@track.tydline.com

Only emails addressed to *@track.tydline.com are processed — all others are
acknowledged and silently dropped.
"""

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_db
from app.services.email_ingest import process_inbound_email

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/email", tags=["email"])

DbSessionDep = Annotated[AsyncSession, Depends(get_db)]

_INBOUND_DOMAIN = "tydline.com"
_TRACKING_SUFFIX = ".track"       # local part must end with .track  e.g. lele.track@tydline.com
_SVIX_TOLERANCE_SECONDS = 300     # reject webhooks older than 5 minutes


def _verify_resend_signature(body: bytes, request: Request) -> None:
    """
    Verify the Svix webhook signature sent by Resend.
    Only enforced when RESEND_WEBHOOK_SECRET is set in .env.
    """
    secret = settings.resend_webhook_secret
    if not secret:
        return

    svix_id = request.headers.get("svix-id", "")
    svix_ts = request.headers.get("svix-timestamp", "")
    svix_sig = request.headers.get("svix-signature", "")

    if not svix_id or not svix_ts or not svix_sig:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Svix headers")

    try:
        if abs(time.time() - int(svix_ts)) > _SVIX_TOLERANCE_SECONDS:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Webhook timestamp too old")
    except ValueError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Svix timestamp")

    raw_secret = base64.b64decode(secret.removeprefix("whsec_"))
    signed_content = f"{svix_id}.{svix_ts}.{body.decode()}"
    mac = hmac.new(raw_secret, signed_content.encode(), hashlib.sha256).digest()
    expected = "v1," + base64.b64encode(mac).decode()

    passed = any(
        hmac.compare_digest(expected, sig.strip())
        for sig in svix_sig.split(" ")
    )
    if not passed:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature")


def _is_tracking_recipient(payload: dict[str, Any]) -> bool:
    """
    Return True if at least one recipient address is @track.tydline.com.
    Checks both Resend (data.to list) and Postmark (ToFull / To string) formats.
    """
    def _bare(addr: str) -> str:
        addr = addr.strip().lower()
        if "<" in addr:
            addr = addr.split("<")[-1].rstrip(">").strip()
        return addr

    # Resend format
    data = payload.get("data") if isinstance(payload.get("data"), dict) else None
    if data:
        recipients = data.get("to") or []
        if isinstance(recipients, str):
            recipients = [recipients]
        cc = data.get("cc") or []
        if isinstance(cc, str):
            cc = [cc]
        all_addrs = [_bare(a) for a in recipients + cc]
    else:
        # Postmark format
        all_addrs = []
        for entry in (payload.get("ToFull") or []) + (payload.get("CcFull") or []):
            if isinstance(entry, dict) and entry.get("Email"):
                all_addrs.append(_bare(entry["Email"]))
        raw_to = payload.get("To", "")
        if raw_to:
            all_addrs.append(_bare(raw_to))

    return any(
        addr.endswith(f"@{_INBOUND_DOMAIN}") and addr.split("@")[0].endswith(_TRACKING_SUFFIX)
        for addr in all_addrs
    )


@router.post("/inbound", status_code=status.HTTP_200_OK)
async def inbound_email_webhook(
    request: Request,
    db: DbSessionDep,
) -> dict[str, Any]:
    """
    Receive a parsed inbound email from Resend, verify the signature, and
    process only emails addressed to *@track.tydline.com.
    """
    body = await request.body()
    _verify_resend_signature(body, request)

    try:
        payload: dict[str, Any] = json.loads(body)
    except Exception:
        logger.warning("Inbound email webhook: could not parse JSON body")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON payload")

    if not _is_tracking_recipient(payload):
        logger.info("Inbound email not addressed to *%s@%s — dropping", _TRACKING_SUFFIX, _INBOUND_DOMAIN)
        return {"status": "ignored", "reason": "not a tracking address"}

    logger.info(
        "Inbound email received — From: %s, Subject: %s",
        (payload.get("data") or payload).get("from") or payload.get("From", "?"),
        (payload.get("data") or payload).get("subject") or payload.get("Subject", "?"),
    )

    try:
        record = await process_inbound_email(db, payload)
    except Exception:
        logger.exception("Failed to process inbound email")
        return {"status": "error", "detail": "processing failed — logged"}

    return {
        "status": "ok",
        "email_id": str(record.id),
        "user_matched": record.user_id is not None,
        "containers_found": record.container_numbers or [],
        "shipments_linked": record.matched_shipment_ids or [],
        "mem0_stored": record.mem0_stored,
    }
