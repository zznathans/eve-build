import secrets

import aio_pika
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import Settings, get_settings
from app.db.mongo import get_database
from app.db.rabbitmq import declare_market_order_queues, get_rabbitmq
from app.services import market_prices

router = APIRouter(prefix="/market-prices", tags=["market-prices"])


def _parse_type_ids(type_ids: str | None) -> set[int] | None:
    if not type_ids:
        return None
    return {int(value) for value in type_ids.split(",") if value}


@router.get("")
async def list_prices(
    db: AsyncIOMotorDatabase = Depends(get_database),
    type_ids: str | None = Query(default=None),
) -> list[dict[str, object]]:
    return await market_prices.list_market_prices(db, _parse_type_ids(type_ids))


@router.get("/{type_id}")
async def get_price(
    type_id: int, db: AsyncIOMotorDatabase = Depends(get_database)
) -> dict[str, object]:
    price = await market_prices.get_market_price(db, type_id)
    if price is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Market price not found")
    return price


@router.post("/refresh")
async def refresh_prices(
    db: AsyncIOMotorDatabase = Depends(get_database),
    settings: Settings = Depends(get_settings),
    rabbitmq_connection: aio_pika.abc.AbstractRobustConnection | None = Depends(get_rabbitmq),
    x_api_key: str | None = Header(default=None),
) -> JSONResponse:
    if not settings.market_prices_refresh_api_key or not secrets.compare_digest(
        x_api_key or "", settings.market_prices_refresh_api_key
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing API key")

    if rabbitmq_connection is None:
        # No queue infra to hand this off to - fall back to the old synchronous behavior
        # rather than failing the request outright.
        upserted = await market_prices.refresh_market_prices(db, settings)
        return JSONResponse({"upserted": upserted})

    channel = await rabbitmq_connection.channel()
    try:
        await declare_market_order_queues(channel)

        async def publish(queue_name: str, body: bytes) -> None:
            await channel.default_exchange.publish(
                aio_pika.Message(body, delivery_mode=aio_pika.DeliveryMode.PERSISTENT),
                routing_key=queue_name,
            )

        scrape_run_id = await market_prices.dispatch_refresh(publish)
    finally:
        await channel.close()

    return JSONResponse(
        {"status": "queued", "scrape_run_id": scrape_run_id},
        status_code=status.HTTP_202_ACCEPTED,
    )
