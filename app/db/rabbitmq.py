import json
from dataclasses import asdict, dataclass
from typing import Any

import aio_pika
from fastapi import Request

from app.core.config import Settings

MARKET_ORDERS_SCRAPE_JOBS_QUEUE = "market_orders.scrape_jobs"
MARKET_ORDERS_RESULTS_QUEUE = "market_orders.results"


async def create_rabbitmq_connection(
    settings: Settings,
) -> aio_pika.abc.AbstractRobustConnection | None:
    if not settings.rabbitmq_enabled:
        return None
    return await aio_pika.connect_robust(settings.rabbitmq_url)


def get_rabbitmq(request: Request) -> aio_pika.abc.AbstractRobustConnection | None:
    return request.app.state.rabbitmq


async def declare_market_order_queues(
    channel: aio_pika.abc.AbstractChannel,
) -> tuple[aio_pika.abc.AbstractQueue, aio_pika.abc.AbstractQueue]:
    scrape_jobs_queue = await channel.declare_queue(MARKET_ORDERS_SCRAPE_JOBS_QUEUE, durable=True)
    results_queue = await channel.declare_queue(MARKET_ORDERS_RESULTS_QUEUE, durable=True)
    return scrape_jobs_queue, results_queue


@dataclass(frozen=True)
class ScrapeJobMessage:
    region_id: int
    scrape_run_id: str
    # Lets a consumer tell this apart from PriceRefreshJobMessage on the same queue before
    # fully decoding - defaults to "orders" so messages published before this field existed
    # still decode.
    kind: str = "orders"


@dataclass(frozen=True)
class OrdersChunkMessage:
    region_id: int
    scrape_run_id: str
    orders: list[dict[str, Any]]
    kind: str = "orders"


@dataclass(frozen=True)
class PriceRefreshJobMessage:
    """A one-off "refresh the averaged/adjusted market prices" job, published to the same
    queue as ScrapeJobMessage (see market_orders.dispatch_scrape) so market data updates as
    one coordinated hourly batch instead of a separate pipeline."""

    scrape_run_id: str
    kind: str = "prices"


@dataclass(frozen=True)
class PriceRefreshResultMessage:
    scrape_run_id: str
    prices: list[dict[str, Any]]
    kind: str = "prices"


def encode_scrape_job(message: ScrapeJobMessage) -> bytes:
    return json.dumps(asdict(message)).encode("utf-8")


def decode_scrape_job(payload: bytes) -> ScrapeJobMessage:
    return ScrapeJobMessage(**json.loads(payload))


def encode_orders_chunk(message: OrdersChunkMessage) -> bytes:
    return json.dumps(asdict(message)).encode("utf-8")


def decode_orders_chunk(payload: bytes) -> OrdersChunkMessage:
    return OrdersChunkMessage(**json.loads(payload))


def encode_price_refresh_job(message: PriceRefreshJobMessage) -> bytes:
    return json.dumps(asdict(message)).encode("utf-8")


def decode_price_refresh_job(payload: bytes) -> PriceRefreshJobMessage:
    return PriceRefreshJobMessage(**json.loads(payload))


def encode_price_refresh_result(message: PriceRefreshResultMessage) -> bytes:
    return json.dumps(asdict(message)).encode("utf-8")


def decode_price_refresh_result(payload: bytes) -> PriceRefreshResultMessage:
    return PriceRefreshResultMessage(**json.loads(payload))


def decode_job(payload: bytes) -> ScrapeJobMessage | PriceRefreshJobMessage:
    """Peeks `kind` on a scrape_jobs-queue message to pick which dataclass to decode into -
    used by the fetch worker, which consumes both job types off the same queue."""
    data = json.loads(payload)
    if data.get("kind") == "prices":
        return PriceRefreshJobMessage(**data)
    return ScrapeJobMessage(**data)


def decode_result(payload: bytes) -> OrdersChunkMessage | PriceRefreshResultMessage:
    """Peeks `kind` on a results-queue message to pick which dataclass to decode into - used
    by the write worker, which consumes both result types off the same queue."""
    data = json.loads(payload)
    if data.get("kind") == "prices":
        return PriceRefreshResultMessage(**data)
    return OrdersChunkMessage(**data)
