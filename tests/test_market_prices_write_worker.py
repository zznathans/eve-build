from collections.abc import AsyncIterator
from typing import Any

import pytest

import app.scripts.market_prices_write_worker as write_worker_module
from app.core.config import get_settings
from app.db.rabbitmq import PriceRefreshResultMessage


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


class _FakeResultsQueue:
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


async def test_main_closes_mongo_client_when_message_handling_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RABBITMQ_ENABLED", "true")
    get_settings.cache_clear()

    fake_client = _FakeMongoClient()
    monkeypatch.setattr(write_worker_module, "create_mongo_client", lambda settings: fake_client)

    async def _fake_create_rabbitmq_connection(settings: object) -> _FakeConnection:
        return _FakeConnection()

    monkeypatch.setattr(
        write_worker_module, "create_rabbitmq_connection", _fake_create_rabbitmq_connection
    )

    fake_queue = _FakeResultsQueue([_FakeMessage(b"irrelevant")])

    async def _fake_declare_market_prices_queues(channel: object) -> tuple[None, _FakeResultsQueue]:
        return None, fake_queue

    monkeypatch.setattr(
        write_worker_module, "declare_market_prices_queues", _fake_declare_market_prices_queues
    )
    monkeypatch.setattr(write_worker_module, "decode_price_refresh_result", lambda body: body)

    async def _boom(db: object, result: object) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(write_worker_module, "apply_price_refresh", _boom)

    with pytest.raises(RuntimeError, match="boom"):
        await write_worker_module.main()

    assert fake_client.closed is True

    get_settings.cache_clear()


async def test_main_applies_price_refresh_for_each_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RABBITMQ_ENABLED", "true")
    get_settings.cache_clear()

    fake_client = _FakeMongoClient()
    monkeypatch.setattr(write_worker_module, "create_mongo_client", lambda settings: fake_client)

    async def _fake_create_rabbitmq_connection(settings: object) -> _FakeConnection:
        return _FakeConnection()

    monkeypatch.setattr(
        write_worker_module, "create_rabbitmq_connection", _fake_create_rabbitmq_connection
    )

    fake_queue = _FakeResultsQueue([_FakeMessage(b"irrelevant")])

    async def _fake_declare_market_prices_queues(channel: object) -> tuple[None, _FakeResultsQueue]:
        return None, fake_queue

    monkeypatch.setattr(
        write_worker_module, "declare_market_prices_queues", _fake_declare_market_prices_queues
    )

    price_result = PriceRefreshResultMessage(refresh_id="run-1", prices=[])
    monkeypatch.setattr(
        write_worker_module, "decode_price_refresh_result", lambda body: price_result
    )

    calls: list[object] = []

    async def _fake_apply_price_refresh(db: object, result: object) -> int:
        calls.append(result)
        return 0

    monkeypatch.setattr(write_worker_module, "apply_price_refresh", _fake_apply_price_refresh)

    await write_worker_module.main()

    assert calls == [price_result]
    assert fake_client.closed is True

    get_settings.cache_clear()
