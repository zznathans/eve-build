import asyncio
import logging
import socket
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.core.config import Settings

logger = logging.getLogger("eve-build.startup_lock")

_LOCK_ID = "startup"
_TTL_INDEX_NAME = "expires_at_ttl"


class LockTimeoutError(Exception):
    """Raised when the startup lock couldn't be acquired within the configured wait time."""


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _ensure_ttl_index(db: AsyncIOMotorDatabase) -> None:
    # Backstop only, for a holder that crashes without releasing - the real
    # reclaim-of-an-expired-lock path is the find_one_and_update filter below,
    # which doesn't wait on Mongo's background TTL sweep (runs ~every 60s).
    await db["_locks"].create_index("expires_at", name=_TTL_INDEX_NAME, expireAfterSeconds=0)


async def _try_acquire(db: AsyncIOMotorDatabase, settings: Settings, holder: str) -> bool:
    now = _now()
    expires_at = now + timedelta(seconds=settings.startup_lock_ttl_seconds)
    try:
        # Matches only "no doc" (upsert inserts one) or "doc exists but expired" - a live
        # lock never matches, so a racing upsert collides on _id and raises DuplicateKeyError,
        # which is the signal that someone else holds the lock.
        result = await db["_locks"].find_one_and_update(
            {"_id": _LOCK_ID, "expires_at": {"$lt": now}},
            {"$set": {"holder": holder, "acquired_at": now, "expires_at": expires_at}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        return False
    return result is not None and result.get("holder") == holder


async def _release(db: AsyncIOMotorDatabase, holder: str) -> None:
    result = await db["_locks"].delete_one({"_id": _LOCK_ID, "holder": holder})
    if result.deleted_count == 0:
        logger.warning(
            "Startup lock was already reclaimed by another holder before release - this "
            "pod's migrations may have raced with a new holder; check "
            "startup_lock_ttl_seconds vs. actual migration duration"
        )


@asynccontextmanager
async def startup_lock(db: AsyncIOMotorDatabase, settings: Settings) -> AsyncIterator[None]:
    await _ensure_ttl_index(db)

    holder = f"{socket.gethostname()}:{uuid.uuid4()}"
    deadline = asyncio.get_event_loop().time() + settings.startup_lock_wait_timeout_seconds

    acquired = await _try_acquire(db, settings, holder)
    if not acquired:
        logger.info("Startup lock held by another pod, waiting")
        while not acquired:
            if asyncio.get_event_loop().time() >= deadline:
                raise LockTimeoutError(
                    f"Timed out after {settings.startup_lock_wait_timeout_seconds}s waiting "
                    "for the startup lock held by another pod"
                )
            await asyncio.sleep(settings.startup_lock_poll_interval_seconds)
            acquired = await _try_acquire(db, settings, holder)
        logger.info("Acquired startup lock")

    try:
        yield
    finally:
        await _release(db, holder)
