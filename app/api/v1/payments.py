"""
Moolre Mobile Money payment endpoints.

POST /api/v1/payments/initiate  — select a plan, start MoMo payment, get OTP sent to phone
POST /api/v1/payments/confirm   — submit OTP to complete payment and activate plan
"""

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_auth_token
from app.core.plans import PURCHASABLE_PLANS, get_plan
from app.db.session import get_db
from app.models.orm import User
from app.services import moolre
from app.services.email import send_email

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/payments", tags=["payments"])

# Test/internal numbers that always pay 10 GHS regardless of plan
_TEST_PHONES = {"233552354808", "233506074801"}
_TEST_AMOUNT = "10.00"


def _resolve_amount(phone: str | None, plan_amount: str) -> str:
    """Return the actual amount to charge — overridden to 10 GHS for test phones."""
    if phone and phone.strip() in _TEST_PHONES:
        return _TEST_AMOUNT
    return plan_amount

DbSessionDep = Annotated[AsyncSession, Depends(get_db)]
CurrentUserDep = Annotated[User, Depends(require_auth_token)]


class InitiatePaymentBody(BaseModel):
    phone: str
    plan: str

    @field_validator("plan")
    @classmethod
    def validate_plan(cls, v: str) -> str:
        v = v.lower()
        if v not in PURCHASABLE_PLANS:
            raise ValueError(f"plan must be one of: {', '.join(sorted(PURCHASABLE_PLANS))}")
        return v


class ConfirmPaymentBody(BaseModel):
    otp_code: str


@router.post("/initiate", status_code=status.HTTP_200_OK)
async def initiate_payment(
    payload: InitiatePaymentBody,
    db: DbSessionDep,
    current_user: CurrentUserDep,
) -> dict:
    """
    Select a plan and initiate a MoMo payment.
    Moolre sends an OTP SMS to the payer's phone.
    The chosen plan is stored as pending until the OTP is confirmed.
    """
    plan_def = get_plan(payload.plan)
    if plan_def is None or plan_def.price_usd is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid plan")

    amount = _resolve_amount(payload.phone, str(plan_def.price_usd))

    if payload.phone.strip() in _TEST_PHONES:
        session_id = f"test-session-{uuid.uuid4().hex[:8]}"
        reference = f"test-ref-{uuid.uuid4().hex[:8]}"
        logger.info("Test phone %s — skipping Moolre, using stub session", payload.phone)
    else:
        try:
            data = await moolre.initiate_payment(
                payer_phone=payload.phone,
                amount=amount,
                external_ref=str(current_user.id),
            )
        except Exception as exc:
            logger.error("Moolre initiate_payment failed: %s", exc)
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Payment initiation failed")

        session_id = data.get("sessionid") or data.get("session_id") or data.get("SessionId")
        reference = data.get("reference") or data.get("Reference")

        if not session_id or not reference:
            logger.error("Moolre response missing sessionid/reference: %s", data)
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Unexpected payment provider response")

    current_user.phone = payload.phone
    current_user.payment_session_id = session_id
    current_user.payment_reference = reference
    current_user.payment_pending_plan = payload.plan
    db.add(current_user)
    await db.commit()

    return {
        "session_id": session_id,
        "plan": payload.plan,
        "amount": amount,
        "message": "Enter the OTP sent to your phone",
    }


@router.post("/confirm", status_code=status.HTTP_200_OK)
async def confirm_payment(
    payload: ConfirmPaymentBody,
    db: DbSessionDep,
    current_user: CurrentUserDep,
) -> dict:
    """
    Submit OTP to complete the MoMo payment.
    On success activates the pending plan and sets subscription_status to 'active'.
    """
    if not current_user.payment_session_id or not current_user.payment_reference:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No pending payment — call /payments/initiate first",
        )

    plan_def = get_plan(current_user.payment_pending_plan)
    if plan_def is None or plan_def.price_usd is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Pending plan is invalid")

    amount = _resolve_amount(current_user.phone, str(plan_def.price_usd))

    if current_user.phone and current_user.phone.strip() in _TEST_PHONES:
        logger.info("Test phone %s — skipping Moolre OTP check, auto-succeeding", current_user.phone)
    else:
        try:
            success = await moolre.complete_payment(
                session_id=current_user.payment_session_id,
                reference=current_user.payment_reference,
                otp_code=payload.otp_code,
                payer_phone=current_user.phone or "",
                amount=amount,
                external_ref=str(current_user.id),
            )
        except Exception as exc:
            logger.error("Moolre complete_payment failed: %s", exc)
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Payment confirmation failed")

        if not success:
            raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail="Payment not successful — check OTP")

    activated_plan = current_user.payment_pending_plan
    current_user.subscription_status = "active"
    current_user.plan = activated_plan
    current_user.payment_session_id = None
    current_user.payment_reference = None
    current_user.payment_pending_plan = None
    db.add(current_user)
    await db.commit()

    # Send receipt email
    plan_name = plan_def.name
    date_str = datetime.now(tz=timezone.utc).strftime("%B %d, %Y")
    text_receipt = (
        f"Payment confirmed — {plan_name} plan\n\n"
        f"Amount paid: GHS {amount}\n"
        f"Billed to: {current_user.email}\n"
        f"Date: {date_str}\n\n"
        f"Go to your dashboard: https://tydline.com/dashboard\n\n"
        f"— The Tydline Team"
    )
    html_receipt: str | None = None
    template_path = Path(__file__).parents[3] / "emails" / "payment-receipt.html"
    try:
        html_receipt = template_path.read_text(encoding="utf-8")
        html_receipt = (
            html_receipt
            .replace("{{plan_name}}", plan_name)
            .replace("{{amount}}", amount)
            .replace("{{email}}", current_user.email)
            .replace("{{date}}", date_str)
        )
    except Exception:
        logger.warning("Could not load payment-receipt email template from %s", template_path)

    await send_email(
        to=current_user.email,
        subject=f"Your Tydline receipt — {plan_name}",
        text_body=text_receipt,
        html_body=html_receipt,
    )

    return {"status": "active", "plan": current_user.plan}
