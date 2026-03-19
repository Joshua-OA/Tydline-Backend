"""
Account endpoints.

GET /api/v1/account/plans  — public plan catalog (pricing page, no auth)
GET /api/v1/account/plan   — current user's active plan + feature flags (auth required)
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.deps import require_auth_token
from app.core.plans import PLANS, PlanDefinition, PlanFeatures, get_user_features
from app.models.orm import User

router = APIRouter(prefix="/account", tags=["account"])

CurrentUserDep = Annotated[User, Depends(require_auth_token)]


class UserPlanResponse(BaseModel):
    subscription_status: str        # pending | active | cancelled
    plan: str | None                # starter | growth | pro | custom | None
    plan_name: str | None
    price_usd: int | None
    features: PlanFeatures | None   # None when subscription is not active


@router.get("/plans", response_model=list[PlanDefinition])
async def list_plans() -> list[PlanDefinition]:
    """
    Public — returns the full plan catalog.
    Use this to render the pricing page before the user signs up.
    """
    return list(PLANS.values())


@router.get("/plan", response_model=UserPlanResponse)
async def get_plan(current_user: CurrentUserDep) -> UserPlanResponse:
    """
    Return the logged-in company's active plan and feature flags.
    features is null when subscription_status is not 'active'.
    Use feature flags to show/hide gated settings in the dashboard.
    """
    from app.core.plans import get_plan as _get_plan

    plan_def = _get_plan(current_user.plan)
    features = get_user_features(current_user.plan, current_user.subscription_status)

    return UserPlanResponse(
        subscription_status=current_user.subscription_status,
        plan=current_user.plan,
        plan_name=plan_def.name if plan_def else None,
        price_usd=plan_def.price_usd if plan_def else None,
        features=features,
    )
