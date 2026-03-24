"""
API v1 router — aggregates all v1 endpoints.
"""

from fastapi import APIRouter

from app.api.v1 import (
    account,
    agent,
    auth,
    coupons,
    dashboard,
    email,
    notify_parties,
    onboarding,
    payments,
    shipments,
    users,
    whatsapp,
)

router = APIRouter(prefix="/api/v1")
router.include_router(auth.router)
router.include_router(account.router)
router.include_router(coupons.router)
router.include_router(dashboard.router)
router.include_router(notify_parties.router)
router.include_router(onboarding.router)
router.include_router(payments.router)
router.include_router(users.router)
router.include_router(shipments.router)
router.include_router(agent.router)
router.include_router(whatsapp.router)
router.include_router(email.router)
