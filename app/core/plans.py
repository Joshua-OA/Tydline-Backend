"""
Plan catalog — single source of truth for plan tiers, pricing, and feature flags.

Imported by account endpoints (display) and payment endpoints (amount derivation).
"""

from pydantic import BaseModel


class PlanFeatures(BaseModel):
    # Channels
    email_notifications: bool
    whatsapp_notifications: bool
    erp_integration: bool
    multi_channel: bool           # False on Starter (choose one only)

    # Limits
    shipment_limit: int | None    # None = unlimited (custom plan)

    # Alerts
    eta_alerts: bool
    eta_change_alerts: bool
    basic_delay_alerts: bool
    custom_alert_rules: bool
    all_alert_types: bool

    # Access
    multi_user_access: bool
    priority_support: bool
    dedicated_account_manager: bool


class PlanDefinition(BaseModel):
    key: str                  # "starter" | "growth" | "pro" | "custom"
    name: str
    price_usd: int | None     # None = negotiated (custom)
    features: PlanFeatures


PLANS: dict[str, PlanDefinition] = {
    "starter": PlanDefinition(
        key="starter",
        name="Starter",
        price_usd=50,
        features=PlanFeatures(
            email_notifications=True,
            whatsapp_notifications=True,   # allowed but only one channel at a time
            erp_integration=False,
            multi_channel=False,           # must choose email OR whatsapp
            shipment_limit=10,
            eta_alerts=True,
            eta_change_alerts=False,
            basic_delay_alerts=True,
            custom_alert_rules=False,
            all_alert_types=False,
            multi_user_access=False,
            priority_support=False,
            dedicated_account_manager=False,
        ),
    ),
    "growth": PlanDefinition(
        key="growth",
        name="Growth",
        price_usd=125,
        features=PlanFeatures(
            email_notifications=True,
            whatsapp_notifications=True,
            erp_integration=False,
            multi_channel=True,
            shipment_limit=40,
            eta_alerts=True,
            eta_change_alerts=True,
            basic_delay_alerts=True,
            custom_alert_rules=True,
            all_alert_types=False,
            multi_user_access=True,
            priority_support=False,
            dedicated_account_manager=False,
        ),
    ),
    "pro": PlanDefinition(
        key="pro",
        name="Pro",
        price_usd=1000,
        features=PlanFeatures(
            email_notifications=True,
            whatsapp_notifications=True,
            erp_integration=True,
            multi_channel=True,
            shipment_limit=450,
            eta_alerts=True,
            eta_change_alerts=True,
            basic_delay_alerts=True,
            custom_alert_rules=True,
            all_alert_types=True,
            multi_user_access=True,
            priority_support=True,
            dedicated_account_manager=True,
        ),
    ),
    "custom": PlanDefinition(
        key="custom",
        name="Custom",
        price_usd=None,
        features=PlanFeatures(
            email_notifications=True,
            whatsapp_notifications=True,
            erp_integration=True,
            multi_channel=True,
            shipment_limit=None,
            eta_alerts=True,
            eta_change_alerts=True,
            basic_delay_alerts=True,
            custom_alert_rules=True,
            all_alert_types=True,
            multi_user_access=True,
            priority_support=True,
            dedicated_account_manager=True,
        ),
    ),
}

# Plans available for self-serve purchase via Moolre
PURCHASABLE_PLANS = {"starter", "growth", "pro"}


def get_plan(plan_key: str | None) -> PlanDefinition | None:
    """Return the PlanDefinition for *plan_key*, or None if not found / no plan."""
    if not plan_key:
        return None
    return PLANS.get(plan_key)


def get_user_features(plan_key: str | None, subscription_status: str) -> PlanFeatures | None:
    """
    Return feature flags for a user.
    Returns None when the user has no active subscription.
    """
    if subscription_status != "active":
        return None
    plan = get_plan(plan_key)
    return plan.features if plan else None
