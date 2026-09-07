import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from prometheus_fastapi_instrumentator import Instrumentator
from starlette.middleware.sessions import SessionMiddleware

from app.core.config import get_settings
from app.db.index_sync import sync_indexes
from app.db.mongo import create_mongo_client
from app.db.rabbitmq import create_rabbitmq_connection
from app.db.redis import create_redis_client
from app.migrations.runner import run_migrations
from app.routes import (
    assets,
    auth,
    blueprints,
    build,
    health,
    jobs,
    market_prices,
    pi,
    planetary,
    plans,
    settings,
)
from app.services.db_gauges import refresh_db_gauges_periodically

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("eve-build")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("Starting up")

    settings = get_settings()
    app.state.settings = settings
    logger.info("Loaded settings (mongodb_database=%s)", settings.mongodb_database)

    app.state.mongo_client = None
    app.state.redis = None
    app.state.rabbitmq = None
    db_gauges_task: asyncio.Task[None] | None = None

    try:
        app.state.mongo_client = create_mongo_client(settings)
        logger.info("MongoDB client created for database %r", settings.mongodb_database)

        app.state.redis = create_redis_client(settings)
        if app.state.redis is not None:
            logger.info("Redis client created (redis_url=%s)", settings.redis_url)
        else:
            logger.info("Redis disabled, skipping cache client")

        app.state.rabbitmq = await create_rabbitmq_connection(settings)
        if app.state.rabbitmq is not None:
            logger.info("RabbitMQ connection established")
        else:
            logger.info("RabbitMQ disabled, skipping connection")

        if settings.run_migrations_on_startup:
            logger.info("Running database migrations")
            await run_migrations(app.state.mongo_client[settings.mongodb_database], settings)
            logger.info("Database migrations complete")
        else:
            logger.info("Skipping database migrations (run_migrations_on_startup=False)")

        if settings.sync_indexes_on_startup:
            logger.info("Syncing MongoDB indexes")
            await sync_indexes(
                app.state.mongo_client[settings.mongodb_database], settings.mongo_indexes_dir
            )
            logger.info("MongoDB index sync complete")
        else:
            logger.info("Skipping MongoDB index sync (sync_indexes_on_startup=False)")

        if settings.metrics_enabled and settings.metrics_db_gauges_enabled:
            db_gauges_task = asyncio.create_task(
                refresh_db_gauges_periodically(
                    app.state.mongo_client[settings.mongodb_database],
                    settings.metrics_gauge_refresh_seconds,
                )
            )
            logger.info(
                "Started DB-derived gauge refresh loop (interval=%ss)",
                settings.metrics_gauge_refresh_seconds,
            )

        logger.info("Startup complete")
        yield
    finally:
        logger.info("Shutting down")
        if db_gauges_task is not None:
            db_gauges_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await db_gauges_task
        if app.state.mongo_client is not None:
            app.state.mongo_client.close()
        if app.state.redis is not None:
            await app.state.redis.aclose()
        if app.state.rabbitmq is not None:
            await app.state.rabbitmq.close()
        logger.info("Shutdown complete")


app = FastAPI(title="eve-build", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

_settings = get_settings()
app.add_middleware(
    SessionMiddleware,
    secret_key=_settings.session_secret_key,
    session_cookie=_settings.session_cookie_name,
    max_age=_settings.session_max_age_seconds,
)

if _settings.metrics_enabled:
    Instrumentator().instrument(app).expose(app)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(assets.router)
app.include_router(blueprints.router)
app.include_router(build.router)
app.include_router(jobs.router)
app.include_router(market_prices.router)
app.include_router(pi.router)
app.include_router(planetary.router)
app.include_router(plans.router)
app.include_router(settings.router)
