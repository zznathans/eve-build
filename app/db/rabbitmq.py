import json
from dataclasses import asdict, dataclass
from typing import Any

import aio_pika
from fastapi import Request

from app.core.config import Settings

MARKET_PRICES_REFRESH_JOBS_QUEUE = "market_prices.refresh_jobs"
MARKET_PRICES_REFRESH_RESULTS_QUEUE = "market_prices.refresh_results"

TRACKED_INFO_REFRESH_JOBS_QUEUE = "tracked_info.refresh_jobs"


async def create_rabbitmq_connection(
    settings: Settings,
) -> aio_pika.abc.AbstractRobustConnection | None:
    if not settings.rabbitmq_enabled:
        return None
    return await aio_pika.connect_robust(settings.rabbitmq_url)


def get_rabbitmq(request: Request) -> aio_pika.abc.AbstractRobustConnection | None:
    return request.app.state.rabbitmq


async def declare_market_prices_queues(
    channel: aio_pika.abc.AbstractChannel,
) -> tuple[aio_pika.abc.AbstractQueue, aio_pika.abc.AbstractQueue]:
    jobs_queue = await channel.declare_queue(MARKET_PRICES_REFRESH_JOBS_QUEUE, durable=True)
    results_queue = await channel.declare_queue(MARKET_PRICES_REFRESH_RESULTS_QUEUE, durable=True)
    return jobs_queue, results_queue


async def declare_tracked_info_queue(
    channel: aio_pika.abc.AbstractChannel,
) -> aio_pika.abc.AbstractQueue:
    return await channel.declare_queue(TRACKED_INFO_REFRESH_JOBS_QUEUE, durable=True)


@dataclass(frozen=True)
class TrackedInfoRefreshJobMessage:
    """kind selects a handler in the refresh worker's dispatch table (e.g.
    "personal_assets", "corp_blueprints") - see app/services/tracked_info.py. character_id is
    whose token to use (for corp_* kinds, the delegate character chosen for that corporation);
    corporation_id is set only for corp_* kinds."""

    kind: str
    character_id: int
    corporation_id: int | None = None


def encode_tracked_info_refresh_job(message: TrackedInfoRefreshJobMessage) -> bytes:
    return json.dumps(asdict(message)).encode("utf-8")


def decode_tracked_info_refresh_job(payload: bytes) -> TrackedInfoRefreshJobMessage:
    return TrackedInfoRefreshJobMessage(**json.loads(payload))


@dataclass(frozen=True)
class PriceRefreshJobMessage:
    refresh_id: str


@dataclass(frozen=True)
class PriceRefreshResultMessage:
    refresh_id: str
    prices: list[dict[str, Any]]


def encode_price_refresh_job(message: PriceRefreshJobMessage) -> bytes:
    return json.dumps(asdict(message)).encode("utf-8")


def decode_price_refresh_job(payload: bytes) -> PriceRefreshJobMessage:
    return PriceRefreshJobMessage(**json.loads(payload))


def encode_price_refresh_result(message: PriceRefreshResultMessage) -> bytes:
    return json.dumps(asdict(message)).encode("utf-8")


def decode_price_refresh_result(payload: bytes) -> PriceRefreshResultMessage:
    return PriceRefreshResultMessage(**json.loads(payload))
