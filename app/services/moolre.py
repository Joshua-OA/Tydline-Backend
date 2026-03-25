"""
Moolre Mobile Money (MoMo) payment integration.

Two-step flow:
  1. initiate_payment  — POST without OTP → Moolre sends OTP SMS to payer
  2. complete_payment  — POST with OTP    → returns True if status == 1 (success)

Auth: X-API-USER + X-API-PUBKEY headers.
"""

import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_MOOLRE_URL = "https://api.moolre.com/open/transact/payment"


def _headers() -> dict[str, str]:
    return {
        "X-API-USER": settings.moolre_api_user or "",
        "X-API-PUBKEY": settings.moolre_public_key or "",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


async def initiate_payment(
    payer_phone: str,
    amount: str,
    external_ref: str,
) -> dict:
    """
    Initiate a MoMo payment (no OTP).  Moolre responds with sessionid + reference
    and sends an OTP SMS to *payer_phone*.

    Returns the parsed JSON response dict (caller should extract sessionid/reference).
    Raises httpx.HTTPStatusError on non-2xx.
    """
    payload = {
        "payerphone": payer_phone,
        "amount": amount,
        "externalref": external_ref,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(_MOOLRE_URL, headers=_headers(), json=payload)
    resp.raise_for_status()
    data = resp.json()
    logger.info("Moolre initiate payment response: %s", data)
    return data


async def complete_payment(
    session_id: str,
    reference: str,
    otp_code: str,
    payer_phone: str,
    amount: str,
    external_ref: str,
) -> bool:
    """
    Complete a MoMo payment with the OTP the payer received via SMS.
    Returns True if Moolre reports status == 1 (success).
    """
    payload = {
        "payerphone": payer_phone,
        "amount": amount,
        "externalref": external_ref,
        "sessionid": session_id,
        "reference": reference,
        "otpcode": otp_code,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(_MOOLRE_URL, headers=_headers(), json=payload)
    resp.raise_for_status()
    data = resp.json()
    logger.info("Moolre complete payment response: %s", data)
    return data.get("status") == 1
