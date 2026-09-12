import logging
from collections.abc import Awaitable, Callable

import httpx
from motor.motor_asyncio import AsyncIOMotorDatabase
from redis.asyncio import Redis

from app.core.config import Settings
from app.db import rabbitmq
from app.models.character import CharacterDocument
from app.services import character_data
from app.services.character_tokens import ensure_fresh_tokens

logger = logging.getLogger("eve-build.tracked_info")

Publish = Callable[[str, bytes], Awaitable[None]]

_PersonalHandler = Callable[
    [AsyncIOMotorDatabase, Redis | None, Settings, str, int], Awaitable[object]
]

# Keyed by each source's cache_key_prefix, which doubles as the job message's `kind` -
# kept in sync with character_data.PERSONAL_SOURCES/CORP_SOURCES automatically, since a new
# entry there needs a matching handler added here to actually be refreshed in the background.
_PERSONAL_HANDLERS: dict[str, _PersonalHandler] = {
    "character_assets": character_data.get_character_assets,
    "character_blueprints": character_data.get_character_blueprints,
    "character_industry_jobs": character_data.get_character_industry_jobs,
    "character_colonies": character_data.get_character_colonies,
}

_CORP_HANDLERS: dict[str, _PersonalHandler] = {
    "corporation_assets": character_data.get_corporation_assets,
    "corporation_blueprints": character_data.get_corporation_blueprints,
    "corporation_industry_jobs": character_data.get_corporation_industry_jobs,
}


async def enqueue_all_refresh_jobs(db: AsyncIOMotorDatabase, publish: Publish) -> int:
    """Enumerates every tracked character and corporation and publishes one refresh job per
    (character, personal source) and per (corporation, corp source) pair - the latter deduped
    to a single delegate character per corporation, since corp data is shared across every
    connected character in that corp (see character_data.cached_corporation_list). Returns the
    number of jobs published."""
    count = 0
    delegate_by_corp: dict[int, int] = {}

    async for raw_doc in db.characters.find({}):
        character = CharacterDocument.model_validate(raw_doc)

        for kind in _PERSONAL_HANDLERS:
            await publish(
                rabbitmq.TRACKED_INFO_REFRESH_JOBS_QUEUE,
                rabbitmq.encode_tracked_info_refresh_job(
                    rabbitmq.TrackedInfoRefreshJobMessage(
                        kind=kind, character_id=character.character_id
                    )
                ),
            )
            count += 1

        if character_data.corp_data_connected(character) and character.corporation_id is not None:
            delegate_by_corp.setdefault(character.corporation_id, character.character_id)

    for corporation_id, delegate_character_id in delegate_by_corp.items():
        for kind in _CORP_HANDLERS:
            await publish(
                rabbitmq.TRACKED_INFO_REFRESH_JOBS_QUEUE,
                rabbitmq.encode_tracked_info_refresh_job(
                    rabbitmq.TrackedInfoRefreshJobMessage(
                        kind=kind,
                        character_id=delegate_character_id,
                        corporation_id=corporation_id,
                    )
                ),
            )
            count += 1

    return count


async def run_refresh_job(
    db: AsyncIOMotorDatabase,
    redis: Redis | None,
    settings: Settings,
    job: rabbitmq.TrackedInfoRefreshJobMessage,
) -> None:
    """Warms the cache for one (character-or-corp, kind) pair by calling the same cache-aware
    getter the request path uses - a no-op if that entry's Redis cache is still warm, an ESI
    fetch + Mongo/Redis rewrite if not. Logs and swallows ESI failures (and missing corp
    role/scope, matching character_data._corp_list_or_none) so one bad character/corp doesn't
    block the queue; it's retried on the next dispatch."""
    raw_doc = await db.characters.find_one({"_id": job.character_id})
    if raw_doc is None:
        logger.warning("Skipping refresh job for unknown character_id=%s", job.character_id)
        return

    character = CharacterDocument.model_validate(raw_doc)
    refreshed = await ensure_fresh_tokens(db, settings, character)
    if refreshed is None:
        logger.info(
            "Skipping refresh job for character_id=%s - refresh token revoked",
            job.character_id,
        )
        return
    character = refreshed

    try:
        if job.corporation_id is None:
            handler = _PERSONAL_HANDLERS.get(job.kind)
            if handler is None:
                logger.warning("Unknown personal refresh kind=%s", job.kind)
                return
            await handler(db, redis, settings, character.access_token, character.character_id)
            return

        handler = _CORP_HANDLERS.get(job.kind)
        if handler is None:
            logger.warning("Unknown corp refresh kind=%s", job.kind)
            return
        if character.corp_access_token is None:
            logger.info(
                "Skipping corp refresh kind=%s for corporation_id=%s - delegate "
                "character_id=%s has no corp token",
                job.kind,
                job.corporation_id,
                job.character_id,
            )
            return
        await handler(db, redis, settings, character.corp_access_token, job.corporation_id)
    except httpx.HTTPStatusError as error:
        if job.corporation_id is not None and error.response.status_code in (401, 403):
            return
        logger.warning(
            "ESI fetch failed for kind=%s character_id=%s corporation_id=%s",
            job.kind,
            job.character_id,
            job.corporation_id,
            exc_info=True,
        )
