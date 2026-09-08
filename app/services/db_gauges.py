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
MARKET_ORDERS_CACHED = Gauge(
    "eve_build_market_orders_cached_total", "Market order entries cached in MongoDB"
)
PLANS_STORED = Gauge(
    "eve_build_plans_stored_total", "Plan entries stored in MongoDB"
)
SDE_BLUEPRINTS_CACHED = Gauge(
    "eve_build_sde_blueprints_cached_total", "SDE blueprint entries cached in MongoDB"
)
SDE_CATEGORIES_CACHED = Gauge(
    "eve_build_sde_categories_cached_total", "SDE category entries cached in MongoDB"
)
SDE_PLANET_SCHEMATICS_CACHED = Gauge(
    "eve_build_sde_planet_schematics_cached_total",
    "SDE planet schematic entries cached in MongoDB",
)
SDE_TYPES_CACHED = Gauge(
    "eve_build_sde_types_cached_total", "SDE type entries cached in MongoDB"
)
LOCATION_NAMES_CACHED = Gauge(
    "eve_build_location_names_cached_total", "Location name entries cached in MongoDB"
)
PLANET_NAMES_CACHED = Gauge(
    "eve_build_planet_names_cached_total", "Planet name entries cached in MongoDB"
)
SYSTEM_NAMES_CACHED = Gauge(
    "eve_build_system_names_cached_total", "System name entries cached in MongoDB"
)
SYSTEM_SECURITY_CACHED = Gauge(
    "eve_build_system_security_cached_total", "System security entries cached in MongoDB"
)


async def refresh_db_gauges(db: AsyncIOMotorDatabase) -> None:
    CHARACTERS_TRACKED.set(await db.characters.count_documents({}))
    MARKET_PRICES_CACHED.set(await db.market_prices.count_documents({}))
    MARKET_ORDERS_CACHED.set(await db.market_orders.count_documents({}))
    PLANS_STORED.set(await db.plans.count_documents({}))
    SDE_BLUEPRINTS_CACHED.set(await db.sde_blueprints.count_documents({}))
    SDE_CATEGORIES_CACHED.set(await db.sde_categories.count_documents({}))
    SDE_PLANET_SCHEMATICS_CACHED.set(await db.sde_planet_schematics.count_documents({}))
    SDE_TYPES_CACHED.set(await db.sde_types.count_documents({}))
    LOCATION_NAMES_CACHED.set(await db.location_names.count_documents({}))
    PLANET_NAMES_CACHED.set(await db.planet_names.count_documents({}))
    SYSTEM_NAMES_CACHED.set(await db.system_names.count_documents({}))
    SYSTEM_SECURITY_CACHED.set(await db.system_security.count_documents({}))


async def refresh_db_gauges_periodically(db: AsyncIOMotorDatabase, interval_seconds: int) -> None:
    while True:
        try:
            await refresh_db_gauges(db)
        except Exception:
            logger.exception("Failed to refresh DB-derived gauges")
        await asyncio.sleep(interval_seconds)
