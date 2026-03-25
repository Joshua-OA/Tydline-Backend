"""
Seed coupon codes into the database.

Usage:
    python scripts/seed_coupons.py

Reads DATABASE_URL from .env (same as the app).
Skips coupons whose code already exists.
"""

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Bootstrap app settings so DATABASE_URL is picked up from .env
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.core.config import settings
from app.models.orm import Coupon

# ---------------------------------------------------------------------------
# Edit this list to add / remove coupons
# ---------------------------------------------------------------------------
COUPONS = [
    {"code": "TYDSTART",   "plan": "starter", "max_uses": None,  "expires_at": None},
    {"code": "TYDGROWTH",  "plan": "growth",  "max_uses": None,  "expires_at": None},
    {"code": "TYDPRO",     "plan": "pro",     "max_uses": None,  "expires_at": None},
    {"code": "EARLYBIRD1", "plan": "growth",  "max_uses": 1,     "expires_at": None},
    {"code": "DEMO2026",   "plan": "starter", "max_uses": 10,    "expires_at": None},
]
# ---------------------------------------------------------------------------


async def seed() -> None:
    engine = create_async_engine(
        settings.database_url,
        echo=False,
        future=True,
        connect_args={"statement_cache_size": 0},
    )
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)

    async with session_factory() as db:
        for entry in COUPONS:
            code = entry["code"].strip().upper()
            result = await db.execute(select(Coupon).where(Coupon.code == code))
            if result.scalar_one_or_none():
                print(f"  skip  {code} (already exists)")
                continue

            coupon = Coupon(
                code=code,
                plan=entry["plan"],
                max_uses=entry["max_uses"],
                expires_at=entry["expires_at"],
            )
            db.add(coupon)
            await db.commit()
            print(f"  added {code} → {entry['plan']}"
                  + (f"  (max {entry['max_uses']} uses)" if entry["max_uses"] else ""))

    await engine.dispose()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(seed())
