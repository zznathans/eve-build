import asyncio
import logging

from motor.motor_asyncio import AsyncIOMotorDatabase
from prometheus_client import Gauge

logger = logging.getLogger("eve-build")

CHARACTERS_TRACKED = Gauge(
    "eve_build_characters_tracked", "Number of characters with a stored session"
)
MARKET_PRICES_CACHED = Gauge(
    "eve_build_market_prices_cached_total", "Market price entries cached in MongoDB"
)


async def refresh_db_gauges(db: AsyncIOMotorDatabase) -> None:
    CHARACTERS_TRACKED.set(await db.characters.count_documents({}))
    MARKET_PRICES_CACHED.set(await db.market_prices.count_documents({}))
    MARKET_ORDERS_CACHED.set(await db.market_orders.count_documents({}))


async def refresh_db_gauges_periodically(db: AsyncIOMotorDatabase, interval_seconds: int) -> None:
    while True:
        try:
            await refresh_db_gauges(db)
        except Exception:
            logger.exception("Failed to refresh DB-derived gauges")
        await asyncio.sleep(interval_seconds)
