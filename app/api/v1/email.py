"""
Postmark inbound email webhook.

Setup in Postmark:
  1. Create an Inbound Domain (e.g. track.tydline.com) in your Postmark account.
  2. Set the Inbound Webhook URL to: https://your-api.tydline.com/api/v1/email/inbound
  3. Enable HTTP Basic Auth on the webhook and set credentials that match
     POSTMARK_INBOUND_SECRET in .env (format: "username:password").
     The endpoint verifies this as an Authorization: Basic <b64> header.
  4. Importers forward or CC their shipping emails to: anything@track.tydline.com

The webhook receives Postmark's parsed email JSON, matches the sender to a
registered user by email address, extracts container numbers, links shipments,
stores the record, and feeds context into Mem0 for the agent.
"""

import base64
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_db
from app.services.email_ingest import process_inbound_email

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Auth — HTTP Basic Auth header verification
# ---------------------------------------------------------------------------


async def require_postmark_auth(
    authorization: str | None = Header(None, alias="Authorization"),
) -> None:
    """
    Verify Postmark inbound webhook via HTTP Basic Auth.
    Skipped if POSTMARK_INBOUND_SECRET is not set (open in dev).
    """
    if not settings.postmark_inbound_secret:
        return  # no secret configured — skip in dev

    if not authorization or not authorization.startswith("Basic "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
            headers={"WWW-Authenticate": "Basic"},
        )

    try:
        decoded = base64.b64decode(authorization[6:]).decode()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Authorization header encoding",
        )

    if decoded != settings.postmark_inbound_secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook credentials",
        )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(
    prefix="/email",
    tags=["email"],
    dependencies=[Depends(require_postmark_auth)],
)

DbSessionDep = Annotated[AsyncSession, Depends(get_db)]


@router.post("/inbound", status_code=status.HTTP_200_OK)
async def inbound_email_webhook(
    request: Request,
    db: DbSessionDep,
) -> dict[str, Any]:
    """
    Receive a parsed inbound email from Postmark, identify the sender,
    extract container numbers, link to shipments, and store in DB + Mem0.

    Returns a minimal acknowledgement — Postmark considers any 2xx a success.
    """
    try:
        payload: dict[str, Any] = await request.json()
    except Exception:
        logger.warning("Inbound email webhook: could not parse JSON body")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload",
        )

    logger.info(
        "Inbound email received — From: %s, Subject: %s",
        payload.get("From", "?"),
        payload.get("Subject", "?"),
    )

    try:
        record = await process_inbound_email(db, payload)
    except Exception:
        logger.exception("Failed to process inbound email")
        # Return 200 to prevent Postmark from retrying — the failure is ours
        return {"status": "error", "detail": "processing failed — logged"}

    return {
        "status": "ok",
        "email_id": str(record.id),
        "user_matched": record.user_id is not None,
        "containers_found": record.container_numbers or [],
        "shipments_linked": record.matched_shipment_ids or [],
        "mem0_stored": record.mem0_stored,
    }
