"""
Tracking service — ShipsGo v2 Ocean API (two-step flow).

Step 1 — Register container:
    POST /v2/ocean/shipments
    Header: X-Shipsgo-User-Token
    Body:   {"containerNumber": "<XXXX>"}
    → returns a shipment_id

Step 2 — Fetch tracking:
    GET /v2/ocean/shipments/{shipment_id}
    Header: X-Shipsgo-User-Token
    → returns status, vessel, ETA, events

The shipment_id is cached in memory so subsequent polls skip Step 1.
Falls back to a generic tracking provider if ShipsGo is not configured.
"""

import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import logging

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models import orm
from app.services.intelligence import persist_timeline_and_risk
from app.utils.retry import with_retries

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory cache: container_number → ShipsGo shipment_id
# Avoids re-registering on every background poll cycle.
# Cache is warm per server instance; re-registration is safe (idempotent).
# ---------------------------------------------------------------------------
_shipsgo_id_cache: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _shipsgo_base() -> str:
    """Base URL for ShipsGo, e.g. https://api.shipsgo.com/v2"""
    return settings.shipsgo_api_base_url.rstrip("/")


def _shipsgo_headers() -> dict[str, str]:
    """ShipsGo v2 uses X-Shipsgo-User-Token for authentication."""
    return {
        "X-Shipsgo-User-Token": settings.shipsgo_api_key or "",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _fallback_headers() -> dict[str, str]:
    if settings.tracking_api_key:
        return {"Authorization": f"Bearer {settings.tracking_api_key}"}
    return {}


# ---------------------------------------------------------------------------
# ShipsGo two-step flow
# ---------------------------------------------------------------------------

async def _register_container(container_number: str) -> str | None:
    """
    Step 1 — POST /ocean/shipments to register a container.
    Returns the ShipsGo shipment_id string, or None on failure.

    The API is idempotent: submitting an already-registered container
    returns the existing resource rather than creating a duplicate.
    """
    url = f"{_shipsgo_base()}/ocean/shipments"
    payload = {"container_number": container_number}

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            async def _post() -> httpx.Response:
                return await client.post(url, headers=_shipsgo_headers(), json=payload)

            response = await with_retries(_post)
            if response is None:
                logger.warning("shipsgo register_container retries exhausted for %s", container_number)
                return None

            if response.status_code == 401:
                logger.error(
                    "shipsgo register_container: 401 Unauthorized — "
                    "check SHIPSGO_API_KEY (X-Shipsgo-User-Token)"
                )
                return None

            if response.status_code == 404:
                logger.warning(
                    "shipsgo register_container: 404 — "
                    "endpoint %s not found, verify SHIPSGO_API_BASE_URL", url
                )
                return None

            # 409 ALREADY_EXISTS — container was previously registered; extract id from body
            if response.status_code not in (200, 201, 409):
                logger.warning(
                    "shipsgo register_container: HTTP %s for %s — %s",
                    response.status_code, container_number, response.text[:200],
                )
                return None

            try:
                data = response.json()
            except Exception as exc:
                logger.warning("shipsgo register_container JSON parse failed: %s", exc)
                return None

            # Shipment ID may be nested or at root — handle both shapes
            # 409 body: {"message":"ALREADY_EXISTS","shipment":{"id":...}}
            shipment_id = (
                data.get("id")
                or data.get("shipmentId")
                or data.get("shipment_id")
                or (data.get("shipment") or {}).get("id")
                or (data.get("data") or {}).get("id")
                or (data.get("data") or {}).get("shipmentId")
            )

            if not shipment_id:
                logger.warning(
                    "shipsgo register_container: could not find shipment_id in response: %s",
                    str(data)[:300],
                )
                return None

            logger.info(
                "shipsgo registered container %s → shipment_id=%s",
                container_number, shipment_id,
            )
            return str(shipment_id)

    except httpx.RequestError as exc:
        logger.warning("shipsgo register_container network error: %s", exc)
        return None


async def _fetch_shipment_tracking(shipment_id: str) -> dict[str, Any] | None:
    """
    Step 2 — GET /ocean/shipments/{shipment_id}.
    Returns the raw JSON response dict, or None on failure.
    """
    url = f"{_shipsgo_base()}/ocean/shipments/{shipment_id}"

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            async def _get() -> httpx.Response:
                return await client.get(url, headers=_shipsgo_headers())

            response = await with_retries(_get)
            if response is None:
                logger.warning("shipsgo fetch_shipment_tracking retries exhausted for id=%s", shipment_id)
                return None

            if response.status_code == 401:
                logger.error("shipsgo fetch_shipment_tracking: 401 Unauthorized")
                return None

            if response.status_code == 404:
                logger.warning("shipsgo fetch_shipment_tracking: 404 for shipment_id=%s", shipment_id)
                return None

            if not response.is_success:
                logger.warning(
                    "shipsgo fetch_shipment_tracking: HTTP %s for id=%s",
                    response.status_code, shipment_id,
                )
                return None

            try:
                return response.json()
            except Exception as exc:
                logger.warning("shipsgo fetch_shipment_tracking JSON parse failed: %s", exc)
                return None

    except httpx.RequestError as exc:
        logger.warning("shipsgo fetch_shipment_tracking network error: %s", exc)
        return None


def _normalize_ocean_response(container_number: str, raw: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize ShipsGo v2 /ocean/shipments/{id} response into the standard
    TydLine tracking shape.

    ShipsGo ocean response fields (flexible — handles both camelCase and snake_case):
      status / containerStatus / container_status
      arrivalDate / arrival_date / eta / estimated_arrival
      vessel / vesselName / vessel_name
      portOfDischarge / pod / destination / location
      events / milestones / movements
    """
    data = raw if isinstance(raw, dict) else {}

    # ShipsGo wraps the shipment under "shipment" or "data" key
    body = data.get("shipment") or data.get("data") or data

    # --- Status ---
    status = (
        body.get("status")
        or body.get("containerStatus")
        or body.get("container_status")
        or body.get("shipmentStatus")
        or body.get("shipment_status")
    )

    # --- ETA / arrival date ---
    eta_raw = (
        body.get("arrivalDate")
        or body.get("arrival_date")
        or body.get("eta")
        or body.get("estimatedArrival")
        or body.get("estimated_arrival")
        or body.get("estimatedArrivalDate")
    )
    eta: str | None = None
    if eta_raw is not None:
        if hasattr(eta_raw, "isoformat"):
            eta = eta_raw.isoformat()
        elif isinstance(eta_raw, str):
            eta = eta_raw
        else:
            eta = str(eta_raw)

    # --- Port of loading (origin) ---
    pol = body.get("portOfLoading") or body.get("pol") or body.get("origin")
    if isinstance(pol, dict):
        pol = pol.get("name") or pol.get("portName") or pol.get("code")

    # --- Port of discharge (destination) ---
    pod = body.get("portOfDischarge") or body.get("pod") or body.get("destination")
    if isinstance(pod, dict):
        pod = pod.get("name") or pod.get("portName") or pod.get("code")
    location = pod or body.get("location") or body.get("currentLocation")

    # --- Vessel ---
    vessel = body.get("vessel") or body.get("vesselName") or body.get("vessel_name")
    if isinstance(vessel, dict):
        vessel = (
            vessel.get("name")
            or vessel.get("vesselName")
            or vessel.get("vessel_name")
        )

    # --- Latest event as status fallback ---
    events = body.get("events") or body.get("milestones") or body.get("movements") or []
    if not status and events:
        latest = events[0] if isinstance(events[0], dict) else {}
        status = (
            latest.get("event")
            or latest.get("eventType")
            or latest.get("status")
            or latest.get("description")
        )

    return {
        "container_number": container_number,
        "status": status,
        "location": location,
        "eta": eta,
        "predicted_eta": None,
        "vessel": vessel,
        "origin": pol,
        "destination": pod,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def fetch_container_tracking_data(container_number: str) -> dict[str, Any]:
    """
    Fetch container tracking data using the ShipsGo v2 two-step flow:
      1. Register container → get shipment_id  (cached after first call)
      2. Fetch tracking via shipment_id

    Falls back to a generic provider if ShipsGo is not configured.
    Returns a normalized dict: container_number, status, location, eta, vessel.
    """
    return await _do_fetch(container_number)


async def _do_fetch(container_number: str) -> dict[str, Any]:
    """Internal: execute the ShipsGo two-step flow or fall back to generic provider."""

    # ---- ShipsGo path ----
    if settings.shipsgo_api_key:
        # Step 1: get or register shipment_id
        shipment_id = _shipsgo_id_cache.get(container_number)

        if not shipment_id:
            shipment_id = await _register_container(container_number)
            if shipment_id:
                _shipsgo_id_cache[container_number] = shipment_id
            else:
                return {}

        # Step 2: fetch tracking data
        raw = await _fetch_shipment_tracking(shipment_id)

        if raw is None:
            # shipment_id may be stale (e.g. ShipsGo re-indexed) — clear cache and retry once
            logger.info(
                "shipsgo shipment_id=%s returned no data for %s, retrying registration",
                shipment_id, container_number,
            )
            _shipsgo_id_cache.pop(container_number, None)
            shipment_id = await _register_container(container_number)
            if shipment_id:
                _shipsgo_id_cache[container_number] = shipment_id
                raw = await _fetch_shipment_tracking(shipment_id)

        if not raw:
            return {}

        normalized = _normalize_ocean_response(container_number, raw)
        normalized["_raw"] = raw
        return normalized

    # ---- Generic fallback provider ----
    if not (settings.tracking_api_base_url and settings.tracking_api_key):
        return {}

    try:
        async with httpx.AsyncClient(
            base_url=settings.tracking_api_base_url,
            headers=_fallback_headers(),
            timeout=20.0,
        ) as client:
            async def _get() -> httpx.Response:
                return await client.get(
                    "/tracking",
                    params={"container_number": container_number},
                )

            response = await with_retries(_get)
            if response is None or not response.is_success:
                return {}

            raw = response.json()
            return {
                "container_number": container_number,
                "status": raw.get("status"),
                "location": raw.get("location"),
                "eta": raw.get("eta"),
                "predicted_eta": raw.get("predicted_eta"),
                "vessel": raw.get("vessel"),
                "_raw": raw,
            }
    except Exception as exc:
        logger.warning("fallback tracking provider error: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# Shipment lifecycle helpers
# ---------------------------------------------------------------------------

async def initial_track_shipment(shipment_id: uuid.UUID) -> None:
    """
    Called when a shipment is first created to perform an initial tracking lookup.
    Uses its own DB session so it can run independently of request lifecycle.
    """
    async with AsyncSessionLocal() as session:
        shipment = await _get_shipment_or_none(session, shipment_id)
        if not shipment:
            return

        reference = shipment.container_number or shipment.bill_of_lading
        if not reference:
            return

        tracking_data = await fetch_container_tracking_data(reference)
        if not tracking_data:
            return

        await _apply_tracking_update(session, shipment, tracking_data)


async def refresh_all_active_shipments() -> None:
    """
    Used by the background worker to refresh all in-progress shipments.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(orm.Shipment).where(
                orm.Shipment.status.not_in(["delivered", "cancelled"])
            )
        )
        shipments = result.scalars().all()

        for shipment in shipments:
            reference = shipment.container_number or shipment.bill_of_lading
            if not reference:
                continue

            tracking_data = await fetch_container_tracking_data(reference)
            if not tracking_data:
                continue

            await _apply_tracking_update(session, shipment, tracking_data)


async def _get_shipment_or_none(
    session: AsyncSession, shipment_id: uuid.UUID
) -> orm.Shipment | None:
    result = await session.execute(
        select(orm.Shipment).where(orm.Shipment.id == shipment_id)
    )
    return result.scalar_one_or_none()


async def _apply_tracking_update(
    session: AsyncSession,
    shipment: orm.Shipment,
    tracking_data: dict[str, Any],
) -> None:
    """
    Apply normalized tracking data onto a shipment record.
    """
    status = tracking_data.get("status")
    eta_raw = tracking_data.get("eta")
    predicted_eta_raw = tracking_data.get("predicted_eta")
    vessel = tracking_data.get("vessel")
    origin = tracking_data.get("origin")
    destination = tracking_data.get("destination")

    if status is not None:
        shipment.status = status

    if eta_raw is not None:
        try:
            shipment.eta = datetime.fromisoformat(eta_raw)
        except (TypeError, ValueError):
            pass

    if predicted_eta_raw is not None:
        try:
            shipment.predicted_eta = datetime.fromisoformat(predicted_eta_raw)
        except (TypeError, ValueError):
            pass

    if vessel is not None:
        shipment.vessel = vessel
    if origin is not None:
        shipment.origin = origin
    if destination is not None:
        shipment.destination = destination

    shipment.last_updated = datetime.now(timezone.utc)

    raw_tracking = tracking_data.pop("_raw", {})

    await session.commit()
    await session.refresh(shipment)

    await persist_timeline_and_risk(
        session=session,
        shipment=shipment,
        raw_tracking=raw_tracking,
    )
