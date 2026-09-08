import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from mongomock_motor import AsyncMongoMockClient

from app.core.config import Settings
from app.db.startup_lock import LockTimeoutError, startup_lock


def _settings(**overrides: object) -> Settings:
    return Settings(
        startup_lock_ttl_seconds=60,
        startup_lock_poll_interval_seconds=0.01,
        startup_lock_wait_timeout_seconds=1,
        **overrides,
    )


async def test_two_concurrent_holders_never_overlap() -> None:
    db = AsyncMongoMockClient()["test"]
    settings = _settings()
    active = 0
    max_active = 0
    intervals: list[tuple[float, float]] = []

    async def _hold(order: list[str], name: str) -> None:
        nonlocal active, max_active
        async with startup_lock(db, settings):
            active += 1
            max_active = max(max_active, active)
            order.append(f"{name}-enter")
            start = asyncio.get_event_loop().time()
            await asyncio.sleep(0.05)
            intervals.append((start, asyncio.get_event_loop().time()))
            order.append(f"{name}-exit")
            active -= 1

    order: list[str] = []
    await asyncio.gather(_hold(order, "a"), _hold(order, "b"))

    assert max_active == 1
    (a_start, a_end), (b_start, b_end) = intervals
    assert a_end <= b_start or b_end <= a_start


async def test_reclaims_an_expired_lock_promptly() -> None:
    db = AsyncMongoMockClient()["test"]
    settings = _settings()
    now = datetime.now(UTC).replace(tzinfo=None)
    await db["_locks"].insert_one(
        {
            "_id": "startup",
            "holder": "stale-holder",
            "acquired_at": now - timedelta(hours=1),
            "expires_at": now - timedelta(minutes=1),
        }
    )

    start = asyncio.get_event_loop().time()
    async with startup_lock(db, settings):
        pass
    elapsed = asyncio.get_event_loop().time() - start

    assert elapsed < settings.startup_lock_wait_timeout_seconds


async def test_releases_lock_on_success() -> None:
    db = AsyncMongoMockClient()["test"]
    settings = _settings()

    async with startup_lock(db, settings):
        pass

    assert await db["_locks"].find_one({"_id": "startup"}) is None


async def test_releases_lock_on_exception() -> None:
    db = AsyncMongoMockClient()["test"]
    settings = _settings()

    with pytest.raises(RuntimeError):
        async with startup_lock(db, settings):
            raise RuntimeError("boom")

    assert await db["_locks"].find_one({"_id": "startup"}) is None


async def test_times_out_when_lock_is_held_and_not_expired() -> None:
    db = AsyncMongoMockClient()["test"]
    settings = _settings()
    now = datetime.now(UTC).replace(tzinfo=None)
    await db["_locks"].insert_one(
        {
            "_id": "startup",
            "holder": "someone-else",
            "acquired_at": now,
            "expires_at": now + timedelta(hours=1),
        }
    )

    with pytest.raises(LockTimeoutError):
        async with startup_lock(db, settings):
            pass


async def test_creates_ttl_index_on_locks_collection() -> None:
    db = AsyncMongoMockClient()["test"]
    settings = _settings()

    async with startup_lock(db, settings):
        pass

    indexes = await db["_locks"].index_information()
    assert "expires_at_ttl" in indexes
