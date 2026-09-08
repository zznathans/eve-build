"""Long-running worker: consumes price-refresh jobs and publishes results for the write worker
to persist. Run as a Deployment; scale replicas with KEDA off the refresh_jobs queue length (see
app.services.market_prices.run_price_refresh_job for what it does).

Usage:
    python -m app.scripts.market_prices_fetch_worker
"""

import asyncio
import logging

import aio_pika

from app.core.config import get_settings
from app.db.rabbitmq import (
    create_rabbitmq_connection,
    declare_market_prices_queues,
    decode_price_refresh_job,
)
from app.services.market_prices import run_price_refresh_job

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("eve-build.market_prices.fetch_worker")


async def main() -> None:
    settings = get_settings()
    connection = await create_rabbitmq_connection(settings)
    if connection is None:
        raise RuntimeError("RabbitMQ is disabled (RABBITMQ_ENABLED=false) - can't run this worker")

    async with connection:
        channel = await connection.channel()
        jobs_queue, _ = await declare_market_prices_queues(channel)

        async def publish(queue_name: str, body: bytes) -> None:
            await channel.default_exchange.publish(
                aio_pika.Message(body, delivery_mode=aio_pika.DeliveryMode.PERSISTENT),
                routing_key=queue_name,
            )

        async with jobs_queue.iterator() as messages:
            async for message in messages:
                job = decode_price_refresh_job(message.body)
                logger.info("Refreshing market prices (refresh_id=%s)", job.refresh_id)
                await run_price_refresh_job(settings, job, publish)
                logger.info("Finished price refresh (refresh_id=%s)", job.refresh_id)
                await message.ack()


if __name__ == "__main__":
    asyncio.run(main())
