"""Long-running worker: consumes price-refresh results and upserts them into `market_prices`.
Run as a Deployment; scale replicas with KEDA off the refresh_results queue length.

Usage:
    python -m app.scripts.market_prices_write_worker
"""

import asyncio
import logging

from app.core.config import get_settings
from app.db.mongo import create_mongo_client
from app.db.rabbitmq import (
    create_rabbitmq_connection,
    declare_market_prices_queues,
    decode_price_refresh_result,
)
from app.services.market_prices import apply_price_refresh

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("eve-build.market_prices.write_worker")


async def main() -> None:
    settings = get_settings()
    connection = await create_rabbitmq_connection(settings)
    if connection is None:
        raise RuntimeError("RabbitMQ is disabled (RABBITMQ_ENABLED=false) - can't run this worker")

    mongo_client = create_mongo_client(settings)
    try:
        db = mongo_client[settings.mongodb_database]

        async with connection:
            channel = await connection.channel()
            await channel.set_qos(prefetch_count=settings.market_prices_write_prefetch)
            _, results_queue = await declare_market_prices_queues(channel)

            async with results_queue.iterator() as messages:
                async for message in messages:
                    result = decode_price_refresh_result(message.body)
                    price_count = await apply_price_refresh(db, result)
                    logger.info("Upserted %s market prices", price_count)
                    await message.ack()
    finally:
        mongo_client.close()


if __name__ == "__main__":
    asyncio.run(main())
