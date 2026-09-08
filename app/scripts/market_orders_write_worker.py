"""Long-running worker: consumes result messages and persists them. Orders arrive one per
message on a per-region queue (see app.db.rabbitmq.market_order_results_queue_name) and are
dumped into the `market_orders` collection - one row per order per scrape run, deduped on
redelivery; old rows are expected to be expired by a TTL index rather than swept here. Price-list
refreshes arrive on their own dedicated queue and are upserted into `market_prices` - see
app.scripts.market_orders_fetch_worker for where both originate.

Discovers the region list at startup (same ESI call app.services.market_orders.dispatch_scrape
uses) and consumes every per-region queue plus the price-refresh queue concurrently in this one
process; run as a Deployment with a fixed replica count - each replica is a competing consumer on
every queue.

Usage:
    python -m app.scripts.market_orders_write_worker
"""

import asyncio
import logging

from aio_pika.abc import AbstractQueue
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import get_settings
from app.db.mongo import create_mongo_client
from app.db.rabbitmq import (
    PriceRefreshResultMessage,
    create_rabbitmq_connection,
    declare_market_order_queues,
    declare_market_order_results_queue,
    decode_order,
    decode_price_refresh_result,
)
from app.services import esi
from app.services.market_orders import apply_order
from app.services.market_prices import apply_price_refresh

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("eve-build.market_orders.write_worker")


async def _consume_orders(queue: AbstractQueue, db: AsyncIOMotorDatabase) -> None:
    async with queue.iterator() as messages:
        async for message in messages:
            result = decode_order(message.body)
            order_count = await apply_order(db, result)
            logger.info("Inserted %s order for region %s", order_count, result.region_id)
            await message.ack()


async def _consume_price_refresh(queue: AbstractQueue, db: AsyncIOMotorDatabase) -> None:
    async with queue.iterator() as messages:
        async for message in messages:
            result: PriceRefreshResultMessage = decode_price_refresh_result(message.body)
            price_count = await apply_price_refresh(db, result)
            logger.info("Upserted %s market prices", price_count)
            await message.ack()


async def main() -> None:
    settings = get_settings()
    connection = await create_rabbitmq_connection(settings)
    if connection is None:
        raise RuntimeError("RabbitMQ is disabled (RABBITMQ_ENABLED=false) - can't run this worker")

    mongo_client = create_mongo_client(settings)
    try:
        db = mongo_client[settings.mongodb_database]
        region_ids = await esi.get_region_ids(settings)

        async with connection:
            channel = await connection.channel()
            await channel.set_qos(prefetch_count=settings.market_orders_write_prefetch)
            _, price_refresh_queue = await declare_market_order_queues(channel)

            try:
                async with asyncio.TaskGroup() as tg:
                    for region_id in region_ids:
                        queue = await declare_market_order_results_queue(channel, region_id)
                        tg.create_task(_consume_orders(queue, db))
                    tg.create_task(_consume_price_refresh(price_refresh_queue, db))
            except* Exception as eg:
                raise eg.exceptions[0] from eg
    finally:
        mongo_client.close()


if __name__ == "__main__":
    asyncio.run(main())
