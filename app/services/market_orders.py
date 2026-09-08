import contextlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import UTC, datetime

import pymongo.errors
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import Settings
from app.db import rabbitmq
from app.services import esi

# Queue name + message payload, so a caller doesn't need aio_pika in scope to publish -
# tests pass a fake that just appends to a list, matching how respx fakes ESI elsewhere.
Publish = Callable[[str, bytes], Awaitable[None]]


async def dispatch_scrape(settings: Settings, publish: Publish) -> str:
    """Enqueues one scrape job per public region, plus one price-refresh job (see
    market_prices.run_price_refresh_job), all under a fresh scrape_run_id - one coordinated
    "update the market" batch per dispatch run. Returns the scrape_run_id so callers (e.g. the
    CLI entrypoint) can log it."""
    scrape_run_id = str(uuid.uuid4())
    region_ids = await esi.get_region_ids(settings)

    for region_id in region_ids:
        message = rabbitmq.ScrapeJobMessage(region_id=region_id, scrape_run_id=scrape_run_id)
        await publish(rabbitmq.MARKET_ORDERS_SCRAPE_JOBS_QUEUE, rabbitmq.encode_scrape_job(message))

    price_job = rabbitmq.PriceRefreshJobMessage(scrape_run_id=scrape_run_id)
    await publish(
        rabbitmq.MARKET_ORDERS_SCRAPE_JOBS_QUEUE, rabbitmq.encode_price_refresh_job(price_job)
    )

    return scrape_run_id


async def run_fetch_job(
    settings: Settings, job: rabbitmq.ScrapeJobMessage, publish: Publish
) -> None:
    """Fetches every page of a region's market orders and publishes each one individually to
    that region's own results queue. Regions with no market (404) yield zero orders/pages."""
    orders: list[esi.MarketOrderEntry] = []

    first_page, total_pages = await esi.get_market_orders_page(settings, job.region_id, 1)
    orders.extend(first_page)

    for page in range(2, total_pages + 1):
        page_orders, _ = await esi.get_market_orders_page(settings, job.region_id, page)
        orders.extend(page_orders)

    queue_name = rabbitmq.market_order_results_queue_name(job.region_id)
    for order in orders:
        order_message = rabbitmq.OrderMessage(
            region_id=job.region_id,
            scrape_run_id=job.scrape_run_id,
            order=asdict(order),
        )
        await publish(queue_name, rabbitmq.encode_order(order_message))


async def apply_order(db: AsyncIOMotorDatabase, message: rabbitmq.OrderMessage) -> int:
    """Inserts one order into market_orders - one row per order per scrape run, deduped on
    redelivery via the unique (order_id, scrape_run_id) index. Old rows are expected to be
    expired by a TTL index rather than swept here."""
    now = datetime.now(UTC).replace(tzinfo=None)

    doc = {
        **message.order,
        "region_id": message.region_id,
        "scrape_run_id": message.scrape_run_id,
        "scraped_at": now,
    }
    with contextlib.suppress(pymongo.errors.DuplicateKeyError):
        await db.market_orders.insert_one(doc)

    return 1
