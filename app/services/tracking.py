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

import re
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import logging

_CONTAINER_RE = re.compile(r"^[A-Z]{4}\d{7}$")

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
    Step 1 — POST /ocean/shipments to register a container or booking/BL reference.
    Returns the ShipsGo shipment_id string, or None on failure.

    ShipsGo accepts either container_number (ISO 6346: XXXX1234567) or booking_number.
    The API is idempotent: submitting an already-registered reference returns the existing resource.
    """
    url = f"{_shipsgo_base()}/ocean/shipments"
    is_container = bool(_CONTAINER_RE.match(container_number.strip().upper()))
    payload = {"container_number": container_number} if is_container else {"booking_number": container_number}

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

    ShipsGo v2 actual structure:
      shipment.status
      shipment.route.port_of_loading.location.name  → origin
      shipment.route.port_of_discharge.location.name → destination
      shipment.route.port_of_discharge.date_of_discharge → eta
      shipment.containers[].movements[].vessel.name  → vessel (latest with a name)
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

    # --- Route block (ShipsGo v2) ---
    route = body.get("route") or {}

    pol_block = route.get("port_of_loading") or {}
    pol_loc = pol_block.get("location") or {}
    pol: str | None = pol_loc.get("name") or pol_loc.get("code")
    # Flat fallbacks for other providers
    if not pol:
        pol_raw = body.get("portOfLoading") or body.get("pol") or body.get("origin")
        if isinstance(pol_raw, dict):
            pol = pol_raw.get("name") or pol_raw.get("portName") or pol_raw.get("code")
        elif isinstance(pol_raw, str):
            pol = pol_raw

    pod_block = route.get("port_of_discharge") or {}
    pod_loc = pod_block.get("location") or {}
    pod: str | None = pod_loc.get("name") or pod_loc.get("code")
    # Flat fallbacks
    if not pod:
        pod_raw = body.get("portOfDischarge") or body.get("pod") or body.get("destination")
        if isinstance(pod_raw, dict):
            pod = pod_raw.get("name") or pod_raw.get("portName") or pod_raw.get("code")
        elif isinstance(pod_raw, str):
            pod = pod_raw

    location = pod or body.get("location") or body.get("currentLocation")

    # --- ETA: ShipsGo v2 stores it on the discharge block ---
    eta_raw: str | None = (
        pod_block.get("date_of_discharge")
        or pod_block.get("date_of_discharge_initial")
        or body.get("arrivalDate")
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

    # --- Vessel: find the latest movement with a vessel name for this container ---
    vessel: str | None = None
    containers = body.get("containers") or []
    # Prefer the specific container; fall back to any container in the shipment
    target_containers = [
        c for c in containers
        if isinstance(c, dict) and c.get("number") == container_number
    ] or [c for c in containers if isinstance(c, dict)]

    for container in target_containers:
        movements = container.get("movements") or []
        for movement in reversed(movements):  # latest first
            if not isinstance(movement, dict):
                continue
            v = movement.get("vessel")
            if isinstance(v, dict):
                name = v.get("name")
                if name:
                    vessel = name
                    break
            elif isinstance(v, str) and v:
                vessel = v
                break
        if vessel:
            break

    # Flat vessel fallback for other providers
    if not vessel:
        v_raw = body.get("vessel") or body.get("vesselName") or body.get("vessel_name")
        if isinstance(v_raw, dict):
            vessel = v_raw.get("name") or v_raw.get("vesselName") or v_raw.get("vessel_name")
        elif isinstance(v_raw, str):
            vessel = v_raw

    # --- Latest event as status fallback ---
    if not status and target_containers:
        movements = target_containers[0].get("movements") or []
        if movements and isinstance(movements[0], dict):
            latest = movements[0]
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

async def fetch_container_tracking_data(
    container_number: str,
    shipsgo_id_hint: str | None = None,
) -> dict[str, Any]:
    """
    Fetch container tracking data using the ShipsGo v2 two-step flow:
      1. Register container → get shipment_id  (cached after first call)
      2. Fetch tracking via shipment_id

    shipsgo_id_hint: previously persisted ShipsGo shipment_id (avoids re-registration).
    Falls back to a generic provider if ShipsGo is not configured.
    Returns a normalized dict: container_number, status, location, eta, vessel.
    The dict may include '_shipsgo_id' with the used shipment_id for persistence.
    """
    return await _do_fetch(container_number, shipsgo_id_hint=shipsgo_id_hint)


async def _do_fetch(
    container_number: str,
    shipsgo_id_hint: str | None = None,
) -> dict[str, Any]:
    """Internal: execute the ShipsGo two-step flow or fall back to generic provider."""

    # ---- ShipsGo path ----
    if settings.shipsgo_api_key:
        # Step 1: resolve shipment_id — DB hint > in-memory cache > register (POST)
        shipment_id = (
            shipsgo_id_hint
            or _shipsgo_id_cache.get(container_number)
        )

        if not shipment_id:
            shipment_id = await _register_container(container_number)
            if shipment_id:
                _shipsgo_id_cache[container_number] = shipment_id
            else:
                return {}
        else:
            # Warm the in-memory cache so subsequent calls in the same process skip the hint
            _shipsgo_id_cache[container_number] = shipment_id

        # Step 2: fetch tracking data
        raw = await _fetch_shipment_tracking(shipment_id)

        # Retry if: no data returned (404/error), OR ShipsGo returned a "NEW" stub
        # (freshly registered shipment that hasn't been processed yet)
        _is_stub = (
            raw is not None
            and (raw.get("shipment") or raw.get("data") or raw).get("status") in ("NEW", None)
            and not (raw.get("shipment") or raw.get("data") or raw).get("route")
        )

        if raw is None or _is_stub:
            logger.info(
                "shipsgo shipment_id=%s returned stub/no data for %s, retrying registration",
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
        normalized["_shipsgo_id"] = shipment_id
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

        # Reuse a known ShipsGo ID from any shipment for the same container (cross-user)
        shipsgo_id_hint = shipment.shipsgo_shipment_id
        if not shipsgo_id_hint:
            existing = await session.execute(
                select(orm.Shipment.shipsgo_shipment_id).where(
                    orm.Shipment.container_number == reference,
                    orm.Shipment.shipsgo_shipment_id.isnot(None),
                    orm.Shipment.id != shipment_id,
                ).limit(1)
            )
            shipsgo_id_hint = existing.scalar_one_or_none()

        tracking_data = await fetch_container_tracking_data(
            reference,
            shipsgo_id_hint=shipsgo_id_hint,
        )
        if not tracking_data:
            from app.services.notification import send_tracking_not_found_notification
            try:
                await send_tracking_not_found_notification(session, shipment)
            except Exception:
                logger.warning(
                    "send_tracking_not_found_notification failed for shipment %s",
                    shipment_id,
                    exc_info=True,
                )
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

        # Build a map of container → shipsgo_id from shipments that already have one
        shipsgo_id_map: dict[str, str] = {
            s.container_number: s.shipsgo_shipment_id
            for s in shipments
            if s.container_number and s.shipsgo_shipment_id
        }

        for shipment in shipments:
            reference = shipment.container_number or shipment.bill_of_lading
            if not reference:
                continue

            # Use own ID, then cross-shipment map (avoids 402 re-registration)
            shipsgo_id_hint = (
                shipment.shipsgo_shipment_id
                or shipsgo_id_map.get(reference)
            )

            tracking_data = await fetch_container_tracking_data(
                reference,
                shipsgo_id_hint=shipsgo_id_hint,
            )
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
    was_tracking_started = shipment.status == "tracking_started"

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

    shipsgo_id = tracking_data.pop("_shipsgo_id", None)
    if shipsgo_id and not shipment.shipsgo_shipment_id:
        shipment.shipsgo_shipment_id = shipsgo_id

    raw_tracking = tracking_data.pop("_raw", {})

    # Fire notify-me email if someone subscribed and real data has arrived
    notify_email = shipment.notify_email
    has_real_data = bool(vessel or origin or destination or eta_raw)
    if notify_email and has_real_data:
        shipment.notify_email = None

    await session.commit()
    await session.refresh(shipment)

    if notify_email and has_real_data:
        from pathlib import Path

        from app.services.email import send_email
        reference = shipment.container_number or shipment.bill_of_lading or ""
        status_str = shipment.status or "In progress"
        vessel_str = shipment.vessel or "Not yet available"
        origin_str = shipment.origin or "Not yet available"
        destination_str = shipment.destination or "Not yet available"
        eta_str = shipment.eta.strftime("%d %b %Y") if shipment.eta else "Not yet available"
        text_body = (
            f"Hi, TASA here. I've picked up live tracking data for {reference}.\n\n"
            f"Status: {status_str}\n"
            f"Vessel: {vessel_str}\n"
            f"Origin: {origin_str}\n"
            f"Destination: {destination_str}\n"
            f"ETA: {eta_str}\n\n"
            "Log in to your Tydline dashboard to see the full details.\n\n— TASA"
        )
        html_body: str | None = None
        template_path = Path(__file__).parent.parent.parent / "emails" / "tracking-live.html"
        try:
            html = template_path.read_text(encoding="utf-8")
            html = html.replace("{{reference}}", reference)
            html = html.replace("{{status}}", status_str)
            html = html.replace("{{vessel}}", vessel_str)
            html = html.replace("{{origin}}", origin_str)
            html = html.replace("{{destination}}", destination_str)
            html = html.replace("{{eta}}", eta_str)
            html = html.replace("{{dashboard_url}}", "https://tydline.com/dashboard")
            html_body = html
        except Exception:
            logger.warning("Could not load tracking-live email template", exc_info=True)
        await send_email(
            to=notify_email,
            subject=f"Your shipment {reference} is now being tracked",
            text_body=text_body,
            html_body=html_body,
        )

    await persist_timeline_and_risk(
        session=session,
        shipment=shipment,
        raw_tracking=raw_tracking,
    )

    # Notify all channels when real data first arrives after approval
    if was_tracking_started and has_real_data:
        from app.services.notification import send_approval_tracking_notification
        try:
            await send_approval_tracking_notification(session, shipment)
        except Exception:
            logger.warning(
                "send_approval_tracking_notification failed for shipment %s",
                shipment.id,
                exc_info=True,
            )
