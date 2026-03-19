"""
Background worker — periodic shipment monitoring cycle.

Workflow:
- Fetch all active shipments from the database
- Refresh container tracking data from ShipsGo
- Detect status changes and trigger notifications
- Update timeline and demurrage risk on each shipment

Run directly:  python -m app.workers.tracker
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db.session import AsyncSessionLocal
from app.models import orm
from app.services.monitor import apply_and_monitor_shipment_update
from app.services.tracking import fetch_container_tracking_data, initial_track_shipment

_AUTO_APPROVE_DAYS = 3

logger = logging.getLogger(__name__)


async def _auto_approve_pending(session) -> None:
    """Approve and start tracking shipments that have been pending for 3+ days."""
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=_AUTO_APPROVE_DAYS)
    result = await session.execute(
        select(orm.Shipment).where(
            orm.Shipment.status == "pending_approval",
            orm.Shipment.created_at <= cutoff,
        )
    )
    pending = result.scalars().all()
    if pending:
        logger.info("auto-approving %d shipment(s) after %d-day window", len(pending), _AUTO_APPROVE_DAYS)
    for shipment in pending:
        shipment.status = "tracking_started"
        session.add(shipment)
    if pending:
        await session.commit()
        for shipment in pending:
            await initial_track_shipment(shipment.id)


async def run_tracker_cycle() -> None:
    """Execute a single monitoring cycle across all active shipments."""
    async with AsyncSessionLocal() as session:
        await _auto_approve_pending(session)

        result = await session.execute(
            select(orm.Shipment)
            .where(orm.Shipment.status.not_in(["delivered", "cancelled", "pending_approval"]))
            .options(selectinload(orm.Shipment.user))
        )
        shipments = result.scalars().all()
        logger.info("tracker cycle started — %d active shipments", len(shipments))

        succeeded = failed = skipped = 0
        for shipment in shipments:
            try:
                tracking_data = await fetch_container_tracking_data(
                    shipment.container_number
                )
                if not tracking_data:
                    skipped += 1
                    continue

                await apply_and_monitor_shipment_update(
                    session=session,
                    shipment=shipment,
                    tracking_data=tracking_data,
                )
                succeeded += 1
            except Exception:
                failed += 1
                logger.exception(
                    "tracker: error processing shipment %s (%s) — skipping",
                    shipment.id,
                    shipment.container_number,
                )

        logger.info(
            "tracker cycle complete — succeeded=%d skipped=%d failed=%d",
            succeeded, skipped, failed,
        )


def main() -> None:
    """Entry point for CLI / cron / serverless runners."""
    asyncio.run(run_tracker_cycle())


if __name__ == "__main__":
    main()
