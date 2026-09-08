"""Long-running worker: consumes jobs from the scrape_jobs queue and publishes results to the
results queue for a write worker to persist. Two job kinds share this queue (see
app.db.rabbitmq.decode_job): a region's market-order scrape (fetches every page from ESI,
retrying transient errors and backing off on ESI's error-rate limit) and a price-list refresh
(fetches ESI's current averaged/adjusted prices) - both published together each hourly dispatch
run, see app.services.market_orders.dispatch_scrape. Run as a Deployment; scale replicas to
parallelize across regions.

Usage:
    python -m app.scripts.market_orders_fetch_worker
"""

import asyncio
import logging

import aio_pika

from app.core.config import get_settings
from app.db.rabbitmq import (
    PriceRefreshJobMessage,
    create_rabbitmq_connection,
    declare_market_order_queues,
    decode_job,
)
from app.services.market_orders import run_fetch_job
from app.services.market_prices import run_price_refresh_job

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("eve-build.market_orders.fetch_worker")


async def main() -> None:
    settings = get_settings()
    connection = await create_rabbitmq_connection(settings)
    if connection is None:
        raise RuntimeError("RabbitMQ is disabled (RABBITMQ_ENABLED=false) - can't run this worker")

    async with connection:
        channel = await connection.channel()
        scrape_jobs_queue, _ = await declare_market_order_queues(channel)

        # Fetch jobs fan out to per-region results queues (see
        # app.db.rabbitmq.market_order_results_queue_name) that aren't declared upfront - the
        # default exchange silently drops a publish to a queue name nothing has declared yet, so
        # declare each one lazily on first use.
        declared_queues: set[str] = set()

        async def publish(queue_name: str, body: bytes) -> None:
            if queue_name not in declared_queues:
                await channel.declare_queue(queue_name, durable=True)
                declared_queues.add(queue_name)
            await channel.default_exchange.publish(
                aio_pika.Message(body, delivery_mode=aio_pika.DeliveryMode.PERSISTENT),
                routing_key=queue_name,
            )

        async with scrape_jobs_queue.iterator() as messages:
            async for message in messages:
                job = decode_job(message.body)
                # Ack only after every downstream publish succeeds - a crash mid-job leaves it
                # unacked, so RabbitMQ redelivers it and another worker retries. Safe: writes
                # downstream are idempotent either way.
                if isinstance(job, PriceRefreshJobMessage):
                    logger.info("Refreshing market prices (scrape_run_id=%s)", job.scrape_run_id)
                    await run_price_refresh_job(settings, job, publish)
                    logger.info("Finished price refresh (scrape_run_id=%s)", job.scrape_run_id)
                else:
                    logger.info("Fetching market orders for region %s", job.region_id)
                    await run_fetch_job(settings, job, publish)
                    logger.info("Finished region %s", job.region_id)
                await message.ack()


if __name__ == "__main__":
    asyncio.run(main())
