"""Long-running worker: consumes tracked-info refresh jobs, warming each character's/corp's
Redis cache by calling the same cache-aware ESI getter the request path uses (see
app.services.tracked_info.run_refresh_job). Run as a Deployment; scale replicas with KEDA off
the refresh_jobs queue length.

Usage:
    python -m app.scripts.tracked_info_refresh_worker
"""

import asyncio
import logging

from app.core.config import get_settings
from app.db.mongo import create_mongo_client
from app.db.rabbitmq import (
    create_rabbitmq_connection,
    declare_tracked_info_queue,
    decode_tracked_info_refresh_job,
)
from app.db.redis import create_redis_client
from app.services.tracked_info import run_refresh_job

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("eve-build.tracked_info.refresh_worker")


async def main() -> None:
    settings = get_settings()
    connection = await create_rabbitmq_connection(settings)
    if connection is None:
        raise RuntimeError("RabbitMQ is disabled (RABBITMQ_ENABLED=false) - can't run this worker")

    mongo_client = create_mongo_client(settings)
    redis = create_redis_client(settings)
    try:
        db = mongo_client[settings.mongodb_database]

        async with connection:
            channel = await connection.channel()
            await channel.set_qos(prefetch_count=settings.tracked_info_refresh_prefetch)
            jobs_queue = await declare_tracked_info_queue(channel)

            async with jobs_queue.iterator() as messages:
                async for message in messages:
                    job = decode_tracked_info_refresh_job(message.body)
                    logger.info(
                        "Refreshing kind=%s character_id=%s corporation_id=%s",
                        job.kind,
                        job.character_id,
                        job.corporation_id,
                    )
                    await run_refresh_job(db, redis, settings, job)
                    await message.ack()
    finally:
        if redis is not None:
            await redis.aclose()
        mongo_client.close()


if __name__ == "__main__":
    asyncio.run(main())
