"""One-shot dispatcher: enqueues an hourly market-price refresh job. Run periodically by the
market-prices-dispatch CronJob; the actual refresh happens in market_prices_fetch_worker.py,
consuming the queue this publishes to.

Usage:
    python -m app.scripts.dispatch_market_prices_refresh
"""

import asyncio
import logging

import aio_pika

from app.core.config import get_settings
from app.db.rabbitmq import create_rabbitmq_connection, declare_market_prices_queues
from app.services.market_prices import dispatch_refresh

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("eve-build.market_prices.dispatch")


async def main() -> None:
    settings = get_settings()
    connection = await create_rabbitmq_connection(settings)
    if connection is None:
        raise RuntimeError(
            "RabbitMQ is disabled (RABBITMQ_ENABLED=false) - can't dispatch a refresh"
        )

    async with connection:
        channel = await connection.channel()
        await declare_market_prices_queues(channel)

        async def publish(queue_name: str, body: bytes) -> None:
            await channel.default_exchange.publish(
                aio_pika.Message(body, delivery_mode=aio_pika.DeliveryMode.PERSISTENT),
                routing_key=queue_name,
            )

        refresh_id = await dispatch_refresh(publish)
        logger.info("Dispatched market price refresh %s", refresh_id)


if __name__ == "__main__":
    asyncio.run(main())
