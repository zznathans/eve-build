from collections.abc import AsyncIterator
from typing import Any

import pytest

import app.scripts.market_orders_write_worker as write_worker_module
from app.core.config import get_settings


class _FakeMongoClient:
    def __init__(self) -> None:
        self.closed = False

    def __getitem__(self, name: str) -> object:
        return object()

    def close(self) -> None:
        self.closed = True


class _FakeMessage:
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def ack(self) -> None:
        pass


class _FakeIteratorCM:
    def __init__(self, messages: list[_FakeMessage]) -> None:
        self._messages = messages

    async def __aenter__(self) -> AsyncIterator[_FakeMessage]:
        async def _gen() -> AsyncIterator[_FakeMessage]:
            for message in self._messages:
                yield message

        return _gen()

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _FakeQueue:
    def __init__(self, messages: list[_FakeMessage]) -> None:
        self._messages = messages

    def iterator(self) -> _FakeIteratorCM:
        return _FakeIteratorCM(self._messages)


class _FakeChannel:
    async def set_qos(self, prefetch_count: int) -> None:
        pass


class _FakeConnection:
    async def __aenter__(self) -> "_FakeConnection":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def channel(self) -> _FakeChannel:
        return _FakeChannel()


def _patch_common(
    monkeypatch: pytest.MonkeyPatch,
    *,
    region_ids: list[int],
    region_messages: dict[int, list[_FakeMessage]] | None = None,
    price_refresh_messages: list[_FakeMessage] | None = None,
) -> _FakeMongoClient:
    monkeypatch.setenv("RABBITMQ_ENABLED", "true")
    get_settings.cache_clear()

    fake_client = _FakeMongoClient()
    monkeypatch.setattr(write_worker_module, "create_mongo_client", lambda settings: fake_client)

    async def _fake_create_rabbitmq_connection(settings: object) -> _FakeConnection:
        return _FakeConnection()

    monkeypatch.setattr(
        write_worker_module, "create_rabbitmq_connection", _fake_create_rabbitmq_connection
    )

    async def _fake_get_region_ids(settings: object) -> list[int]:
        return region_ids

    monkeypatch.setattr(write_worker_module.esi, "get_region_ids", _fake_get_region_ids)

    price_queue = _FakeQueue(price_refresh_messages or [])

    async def _fake_declare_market_order_queues(channel: object) -> tuple[None, _FakeQueue]:
        return None, price_queue

    monkeypatch.setattr(
        write_worker_module, "declare_market_order_queues", _fake_declare_market_order_queues
    )

    region_messages = region_messages or {}

    async def _fake_declare_market_order_results_queue(
        channel: object, region_id: int
    ) -> _FakeQueue:
        return _FakeQueue(region_messages.get(region_id, []))

    monkeypatch.setattr(
        write_worker_module,
        "declare_market_order_results_queue",
        _fake_declare_market_order_results_queue,
    )

    return fake_client


async def test_main_closes_mongo_client_when_message_handling_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _patch_common(
        monkeypatch,
        region_ids=[10000002],
        region_messages={10000002: [_FakeMessage(b"irrelevant")]},
    )
    monkeypatch.setattr(write_worker_module, "decode_order", lambda body: body)

    async def _boom(db: object, message: object) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(write_worker_module, "apply_order", _boom)

    with pytest.raises(RuntimeError, match="boom"):
        await write_worker_module.main()

    assert fake_client.closed is True

    get_settings.cache_clear()


async def test_main_applies_price_refresh_for_a_price_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _patch_common(
        monkeypatch,
        region_ids=[10000002],
        price_refresh_messages=[_FakeMessage(b"irrelevant")],
    )

    price_result = write_worker_module.PriceRefreshResultMessage(scrape_run_id="run-1", prices=[])
    monkeypatch.setattr(
        write_worker_module, "decode_price_refresh_result", lambda body: price_result
    )

    calls: list[object] = []

    async def _fake_apply_price_refresh(db: object, result: object) -> int:
        calls.append(result)
        return 0

    async def _unexpected_apply_order(db: object, message: object) -> int:
        raise AssertionError("apply_order should not run for a price-refresh result")

    monkeypatch.setattr(write_worker_module, "apply_price_refresh", _fake_apply_price_refresh)
    monkeypatch.setattr(write_worker_module, "apply_order", _unexpected_apply_order)

    await write_worker_module.main()

    assert calls == [price_result]
    assert fake_client.closed is True

    get_settings.cache_clear()
