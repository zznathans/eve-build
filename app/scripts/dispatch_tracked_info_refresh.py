"""One-shot dispatcher: enumerates every tracked character/corporation and enqueues a
refresh job for each of their tracked data sources (assets, blueprints, industry jobs, PI
colonies). Run periodically by the tracked-info-dispatch CronJob; the actual refresh happens
in tracked_info_refresh_worker.py, consuming the queue this publishes to. Keeps every tracked
character's/corp's Redis cache warm so a page load almost always hits Redis instead of
blocking on ESI.

Usage:
    python -m app.scripts.dispatch_tracked_info_refresh
"""

import asyncio
import logging

import aio_pika

from app.core.config import get_settings
from app.db.mongo import create_mongo_client
from app.db.rabbitmq import create_rabbitmq_connection, declare_tracked_info_queue
from app.services.tracked_info import enqueue_all_refresh_jobs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("eve-build.tracked_info.dispatch")


async def main() -> None:
    settings = get_settings()
    connection = await create_rabbitmq_connection(settings)
    if connection is None:
        raise RuntimeError(
            "RabbitMQ is disabled (RABBITMQ_ENABLED=false) - can't dispatch a refresh"
        )

    mongo_client = create_mongo_client(settings)
    try:
        db = mongo_client[settings.mongodb_database]

        async with connection:
            channel = await connection.channel()
            await declare_tracked_info_queue(channel)

            async def publish(queue_name: str, body: bytes) -> None:
                await channel.default_exchange.publish(
                    aio_pika.Message(body, delivery_mode=aio_pika.DeliveryMode.PERSISTENT),
                    routing_key=queue_name,
                )

            job_count = await enqueue_all_refresh_jobs(db, publish)
            logger.info("Dispatched %s tracked-info refresh jobs", job_count)
    finally:
        mongo_client.close()


if __name__ == "__main__":
    asyncio.run(main())
