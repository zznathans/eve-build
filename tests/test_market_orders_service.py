import respx
from httpx import Response
from mongomock_motor import AsyncMongoMockClient

from app.core.config import Settings
from app.db import rabbitmq
from app.services import market_orders

_SAMPLE_ORDER = {
    "order_id": 1,
    "type_id": 34,
    "location_id": 60003760,
    "is_buy_order": False,
    "price": 5.5,
    "volume_remain": 100,
    "volume_total": 200,
    "min_volume": 1,
    "duration": 90,
    "issued": "2026-01-01T00:00:00Z",
    "range": "region",
}


class _RecordingPublisher:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bytes]] = []

    async def __call__(self, queue_name: str, body: bytes) -> None:
        self.messages.append((queue_name, body))


@respx.mock
async def test_dispatch_scrape_enqueues_one_job_per_region() -> None:
    settings = Settings()
    respx.get(f"{settings.esi_base_url}/universe/regions/").mock(
        return_value=Response(200, json=[10000002, 10000043])
    )
    publisher = _RecordingPublisher()

    scrape_run_id = await market_orders.dispatch_scrape(settings, publisher)

    assert len(publisher.messages) == 3
    decoded = [
        rabbitmq.decode_job(body)
        for queue_name, body in publisher.messages
        if queue_name == rabbitmq.MARKET_ORDERS_SCRAPE_JOBS_QUEUE
    ]
    region_jobs = [job for job in decoded if isinstance(job, rabbitmq.ScrapeJobMessage)]
    price_jobs = [job for job in decoded if isinstance(job, rabbitmq.PriceRefreshJobMessage)]
    assert {job.region_id for job in region_jobs} == {10000002, 10000043}
    assert len(price_jobs) == 1
    assert all(job.scrape_run_id == scrape_run_id for job in decoded)


@respx.mock
async def test_run_fetch_job_chunks_orders_and_publishes_them() -> None:
    settings = Settings(market_orders_chunk_size=1)
    respx.get(f"{settings.esi_base_url}/markets/10000002/orders/", params={"page": 1}).mock(
        return_value=Response(200, headers={"X-Pages": "2"}, json=[_SAMPLE_ORDER])
    )
    respx.get(f"{settings.esi_base_url}/markets/10000002/orders/", params={"page": 2}).mock(
        return_value=Response(
            200, headers={"X-Pages": "2"}, json=[{**_SAMPLE_ORDER, "order_id": 2}]
        )
    )
    publisher = _RecordingPublisher()
    job = rabbitmq.ScrapeJobMessage(region_id=10000002, scrape_run_id="run-1")

    await market_orders.run_fetch_job(settings, job, publisher)

    chunk_messages = []
    for queue_name, body in publisher.messages:
        assert queue_name == rabbitmq.MARKET_ORDERS_RESULTS_QUEUE
        chunk_messages.append(rabbitmq.decode_orders_chunk(body))

    assert len(chunk_messages) == 2  # chunk_size=1, two orders fetched
    assert {chunk.orders[0]["order_id"] for chunk in chunk_messages} == {1, 2}


@respx.mock
async def test_run_fetch_job_handles_region_with_no_market() -> None:
    settings = Settings()
    respx.get(f"{settings.esi_base_url}/markets/10000004/orders/", params={"page": 1}).mock(
        return_value=Response(404, json={"error": "Region not found"})
    )
    publisher = _RecordingPublisher()
    job = rabbitmq.ScrapeJobMessage(region_id=10000004, scrape_run_id="run-1")

    await market_orders.run_fetch_job(settings, job, publisher)

    assert publisher.messages == []


async def test_apply_orders_chunk_inserts_one_row_per_order(
    mongo_db: AsyncMongoMockClient,
) -> None:
    message = rabbitmq.OrdersChunkMessage(
        region_id=10000002, scrape_run_id="run-1", orders=[_SAMPLE_ORDER]
    )

    count = await market_orders.apply_orders_chunk(mongo_db, message)

    assert count == 1
    order = await mongo_db.market_orders.find_one({"order_id": 1, "scrape_run_id": "run-1"})
    assert order is not None
    assert order["region_id"] == 10000002
    assert order["price"] == 5.5


async def test_apply_orders_chunk_ignores_duplicates_on_redelivery(
    mongo_db: AsyncMongoMockClient,
) -> None:
    await mongo_db.market_orders.create_index([("order_id", 1), ("scrape_run_id", 1)], unique=True)
    message = rabbitmq.OrdersChunkMessage(
        region_id=10000002, scrape_run_id="run-1", orders=[_SAMPLE_ORDER]
    )

    await market_orders.apply_orders_chunk(mongo_db, message)
    await market_orders.apply_orders_chunk(mongo_db, message)  # redelivered chunk

    order_count = await mongo_db.market_orders.count_documents(
        {"order_id": 1, "scrape_run_id": "run-1"}
    )
    assert order_count == 1
