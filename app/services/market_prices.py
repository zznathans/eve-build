import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import UTC, datetime
from typing import cast

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import Settings
from app.db import rabbitmq
from app.services import esi

Publish = Callable[[str, bytes], Awaitable[None]]


async def _upsert_price(
    db: AsyncIOMotorDatabase, entry: esi.MarketPriceEntry, now: datetime
) -> None:
    await db.market_prices.update_one(
        {"_id": entry.type_id},
        {
            "$set": {
                "adjusted_price": entry.adjusted_price,
                "average_price": entry.average_price,
                "updated_at": now,
            }
        },
        upsert=True,
    )


async def _upsert_prices(db: AsyncIOMotorDatabase, entries: list[esi.MarketPriceEntry]) -> int:
    now = datetime.now(UTC).replace(tzinfo=None)
    await asyncio.gather(*(_upsert_price(db, entry, now) for entry in entries))
    return len(entries)


async def refresh_market_prices(db: AsyncIOMotorDatabase, settings: Settings) -> int:
    """Direct fetch-and-upsert, used as the RabbitMQ-disabled fallback for the /refresh
    endpoint - when RabbitMQ is enabled, run_price_refresh_job/apply_price_refresh (below) do
    the same work split across the market_orders fetch/write worker pool instead."""
    entries = await esi.get_market_prices(settings)
    return await _upsert_prices(db, entries)


async def dispatch_refresh(publish: Publish) -> str:
    """Enqueues a one-off price-refresh job onto the market_orders scrape_jobs queue, for the
    manual /market-prices/refresh endpoint - the same job kind the hourly
    market_orders.dispatch_scrape run enqueues automatically. Returns the scrape_run_id."""
    scrape_run_id = str(uuid.uuid4())
    job = rabbitmq.PriceRefreshJobMessage(scrape_run_id=scrape_run_id)
    await publish(rabbitmq.MARKET_ORDERS_SCRAPE_JOBS_QUEUE, rabbitmq.encode_price_refresh_job(job))
    return scrape_run_id


async def run_price_refresh_job(
    settings: Settings, job: rabbitmq.PriceRefreshJobMessage, publish: Publish
) -> None:
    """Fetches the current market price list from ESI and publishes it to the results queue -
    consumed by apply_price_refresh on the write-worker side. Doesn't touch Mongo, matching
    run_fetch_job's "fetch worker never talks to the DB" boundary."""
    entries = await esi.get_market_prices(settings)
    result = rabbitmq.PriceRefreshResultMessage(
        scrape_run_id=job.scrape_run_id,
        prices=[asdict(entry) for entry in entries],
    )
    await publish(
        rabbitmq.MARKET_PRICE_REFRESH_RESULTS_QUEUE, rabbitmq.encode_price_refresh_result(result)
    )


async def apply_price_refresh(
    db: AsyncIOMotorDatabase, result: rabbitmq.PriceRefreshResultMessage
) -> int:
    entries = [esi.MarketPriceEntry(**entry) for entry in result.prices]
    return await _upsert_prices(db, entries)


async def list_market_prices(
    db: AsyncIOMotorDatabase, type_ids: set[int] | None = None
) -> list[dict[str, object]]:
    query = {"_id": {"$in": list(type_ids)}} if type_ids else {}
    return await db.market_prices.find(query).to_list(None)


async def get_market_price(db: AsyncIOMotorDatabase, type_id: int) -> dict[str, object] | None:
    return await db.market_prices.find_one({"_id": type_id})


def unit_price(price_doc: dict[str, object] | None) -> float:
    return float(cast(float | int | None, (price_doc or {}).get("average_price")) or 0.0)
