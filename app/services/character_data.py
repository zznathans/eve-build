import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, NamedTuple, TypeVar, cast

import httpx
from motor.motor_asyncio import AsyncIOMotorDatabase
from redis.asyncio import Redis

from app.core.config import Settings
from app.models.character import CharacterDocument
from app.services import esi, esi_cache

if TYPE_CHECKING:
    from _typeshed import DataclassInstance

    T = TypeVar("T", bound=DataclassInstance)
else:
    T = TypeVar("T")

logger = logging.getLogger("eve-build.character_data")

_ASSETS_CACHE_TTL_SECONDS = 60 * 60
_BLUEPRINTS_CACHE_TTL_SECONDS = 60 * 60
_INDUSTRY_JOBS_CACHE_TTL_SECONDS = 60 * 60
# Shorter than the other sources - extractor/factory expiry timers are time-sensitive,
# and a colony's own list is cheap (few colonies per character, unlike assets/jobs).
_COLONIES_CACHE_TTL_SECONDS = 60 * 15


class _Source(NamedTuple):
    label: str
    collection_name: str
    cache_key_prefix: str


_ASSETS_SOURCE = _Source("Assets", "assets", "character_assets")
_BLUEPRINTS_SOURCE = _Source("Blueprints", "blueprints", "character_blueprints")
_INDUSTRY_JOBS_SOURCE = _Source("Industry jobs", "industry_jobs", "character_industry_jobs")
_COLONIES_SOURCE = _Source("PI colonies", "planetary_colonies", "character_colonies")
_CORP_ASSETS_SOURCE = _Source("Corporation assets", "corp_assets", "corporation_assets")
_CORP_BLUEPRINTS_SOURCE = _Source(
    "Corporation blueprints", "corp_blueprints", "corporation_blueprints"
)
_CORP_INDUSTRY_JOBS_SOURCE = _Source(
    "Corporation industry jobs", "corp_industry_jobs", "corporation_industry_jobs"
)

_PERSONAL_SOURCES = (_ASSETS_SOURCE, _BLUEPRINTS_SOURCE, _INDUSTRY_JOBS_SOURCE, _COLONIES_SOURCE)
_CORP_SOURCES = (_CORP_ASSETS_SOURCE, _CORP_BLUEPRINTS_SOURCE, _CORP_INDUSTRY_JOBS_SOURCE)


def corp_data_connected(character: CharacterDocument) -> bool:
    return character.corp_refresh_token is not None


async def _corp_list_or_none(
    db: AsyncIOMotorDatabase,
    redis: Redis | None,
    *,
    collection_name: str,
    cache_key_prefix: str,
    corporation_id: int,
    ttl_seconds: int,
    entry_type: type[T],
    fetch: Callable[[], Awaitable[list[T]]],
) -> list[T] | None:
    """None means this character can't use this endpoint right now - either a 403
    (valid token, but missing the corp role the endpoint requires, e.g. Director
    for assets/blueprints) or a 401 (the corp token doesn't actually carry the
    scope this endpoint needs, e.g. EVE_SSO_CORP_SCOPES was misconfigured when the
    character connected). [] means the fetch succeeded and the corp just doesn't
    own anything of that kind."""
    try:
        return await esi_cache.cached_corporation_list(
            db,
            redis,
            collection_name=collection_name,
            cache_key_prefix=cache_key_prefix,
            corporation_id=corporation_id,
            ttl_seconds=ttl_seconds,
            entry_type=entry_type,
            fetch=fetch,
        )
    except httpx.HTTPStatusError as error:
        if error.response.status_code in (401, 403):
            return None
        raise


async def get_character_assets(
    db: AsyncIOMotorDatabase,
    redis: Redis | None,
    settings: Settings,
    access_token: str,
    character_id: int,
) -> list[esi.AssetEntry]:
    return await esi_cache.cached_character_list(
        db,
        redis,
        collection_name=_ASSETS_SOURCE.collection_name,
        cache_key_prefix=_ASSETS_SOURCE.cache_key_prefix,
        character_id=character_id,
        ttl_seconds=_ASSETS_CACHE_TTL_SECONDS,
        entry_type=esi.AssetEntry,
        fetch=lambda: esi.get_character_assets(settings, access_token, character_id),
    )


async def get_character_blueprints(
    db: AsyncIOMotorDatabase,
    redis: Redis | None,
    settings: Settings,
    access_token: str,
    character_id: int,
) -> list[esi.BlueprintEntry]:
    return await esi_cache.cached_character_list(
        db,
        redis,
        collection_name=_BLUEPRINTS_SOURCE.collection_name,
        cache_key_prefix=_BLUEPRINTS_SOURCE.cache_key_prefix,
        character_id=character_id,
        ttl_seconds=_BLUEPRINTS_CACHE_TTL_SECONDS,
        entry_type=esi.BlueprintEntry,
        fetch=lambda: esi.get_character_blueprints(settings, access_token, character_id),
    )


async def get_character_industry_jobs(
    db: AsyncIOMotorDatabase,
    redis: Redis | None,
    settings: Settings,
    access_token: str,
    character_id: int,
) -> list[esi.IndustryJobEntry]:
    return await esi_cache.cached_character_list(
        db,
        redis,
        collection_name=_INDUSTRY_JOBS_SOURCE.collection_name,
        cache_key_prefix=_INDUSTRY_JOBS_SOURCE.cache_key_prefix,
        character_id=character_id,
        ttl_seconds=_INDUSTRY_JOBS_CACHE_TTL_SECONDS,
        entry_type=esi.IndustryJobEntry,
        fetch=lambda: esi.get_character_industry_jobs(settings, access_token, character_id),
    )


async def _fetch_colony_records(
    settings: Settings, access_token: str, character_id: int
) -> list[esi.ColonyRecord]:
    """A colony detail fetch failure is isolated to that one planet - it's logged and
    skipped rather than raised, so one bad/unreachable planet doesn't block the rest of
    this character's colonies (which share a single cache entry) from refreshing."""
    summaries = await esi.get_character_colonies(settings, access_token, character_id)
    records = []
    for summary in summaries:
        try:
            detail = await esi.get_character_colony_detail(
                settings, access_token, character_id, summary.planet_id
            )
        except httpx.HTTPError:
            logger.warning(
                "Failed to fetch colony detail (character_id=%s, planet_id=%s)",
                character_id,
                summary.planet_id,
                exc_info=True,
            )
            continue
        records.append(
            esi.ColonyRecord(
                planet_id=summary.planet_id,
                solar_system_id=summary.solar_system_id,
                planet_type=summary.planet_type,
                owner_id=summary.owner_id,
                last_update=summary.last_update,
                upgrade_level=summary.upgrade_level,
                num_pins=summary.num_pins,
                pins=detail.pins,
                links=detail.links,
                routes=detail.routes,
            )
        )
    return records


async def get_character_colonies(
    db: AsyncIOMotorDatabase,
    redis: Redis | None,
    settings: Settings,
    access_token: str,
    character_id: int,
) -> list[esi.ColonyRecord]:
    """No get_merged_colonies/corp equivalent - ESI has no corporation PI endpoint, so
    unlike assets/blueprints/jobs this is personal-only, with nothing to merge."""
    return await esi_cache.cached_character_list(
        db,
        redis,
        collection_name=_COLONIES_SOURCE.collection_name,
        cache_key_prefix=_COLONIES_SOURCE.cache_key_prefix,
        character_id=character_id,
        ttl_seconds=_COLONIES_CACHE_TTL_SECONDS,
        entry_type=esi.ColonyRecord,
        fetch=lambda: _fetch_colony_records(settings, access_token, character_id),
    )


async def get_corporation_assets(
    db: AsyncIOMotorDatabase,
    redis: Redis | None,
    settings: Settings,
    access_token: str,
    corporation_id: int,
) -> list[esi.AssetEntry] | None:
    return await _corp_list_or_none(
        db,
        redis,
        collection_name=_CORP_ASSETS_SOURCE.collection_name,
        cache_key_prefix=_CORP_ASSETS_SOURCE.cache_key_prefix,
        corporation_id=corporation_id,
        ttl_seconds=_ASSETS_CACHE_TTL_SECONDS,
        entry_type=esi.AssetEntry,
        fetch=lambda: esi.get_corporation_assets(settings, access_token, corporation_id),
    )


async def get_corporation_blueprints(
    db: AsyncIOMotorDatabase,
    redis: Redis | None,
    settings: Settings,
    access_token: str,
    corporation_id: int,
) -> list[esi.BlueprintEntry] | None:
    return await _corp_list_or_none(
        db,
        redis,
        collection_name=_CORP_BLUEPRINTS_SOURCE.collection_name,
        cache_key_prefix=_CORP_BLUEPRINTS_SOURCE.cache_key_prefix,
        corporation_id=corporation_id,
        ttl_seconds=_BLUEPRINTS_CACHE_TTL_SECONDS,
        entry_type=esi.BlueprintEntry,
        fetch=lambda: esi.get_corporation_blueprints(settings, access_token, corporation_id),
    )


async def get_corporation_industry_jobs(
    db: AsyncIOMotorDatabase,
    redis: Redis | None,
    settings: Settings,
    access_token: str,
    corporation_id: int,
) -> list[esi.IndustryJobEntry] | None:
    return await _corp_list_or_none(
        db,
        redis,
        collection_name=_CORP_INDUSTRY_JOBS_SOURCE.collection_name,
        cache_key_prefix=_CORP_INDUSTRY_JOBS_SOURCE.cache_key_prefix,
        corporation_id=corporation_id,
        ttl_seconds=_INDUSTRY_JOBS_CACHE_TTL_SECONDS,
        entry_type=esi.IndustryJobEntry,
        fetch=lambda: esi.get_corporation_industry_jobs(settings, access_token, corporation_id),
    )


async def _merge_corp(
    db: AsyncIOMotorDatabase,
    redis: Redis | None,
    settings: Settings,
    character: CharacterDocument,
    personal: list[T],
    fetch_corp: Callable[[str, int], Awaitable[list[T] | None]],
) -> tuple[list[T], bool]:
    """Concatenates personal + corp entries when corp data is connected and the
    character has the role the endpoint requires. Returns whether corp data was
    actually merged in, so routes can show an "includes corporation data" note."""
    if not corp_data_connected(character):
        return personal, False

    corp_entries = await fetch_corp(
        cast(str, character.corp_access_token), cast(int, character.corporation_id)
    )
    if corp_entries is None:
        return personal, False
    return [*personal, *corp_entries], True


async def get_merged_assets(
    db: AsyncIOMotorDatabase,
    redis: Redis | None,
    settings: Settings,
    character: CharacterDocument,
) -> tuple[list[esi.AssetEntry], bool]:
    personal = await get_character_assets(
        db, redis, settings, character.access_token, character.character_id
    )
    return await _merge_corp(
        db,
        redis,
        settings,
        character,
        personal,
        lambda token, corp_id: get_corporation_assets(db, redis, settings, token, corp_id),
    )


async def get_merged_blueprints(
    db: AsyncIOMotorDatabase,
    redis: Redis | None,
    settings: Settings,
    character: CharacterDocument,
) -> tuple[list[esi.BlueprintEntry], bool]:
    personal = await get_character_blueprints(
        db, redis, settings, character.access_token, character.character_id
    )
    return await _merge_corp(
        db,
        redis,
        settings,
        character,
        personal,
        lambda token, corp_id: get_corporation_blueprints(db, redis, settings, token, corp_id),
    )


async def get_merged_industry_jobs(
    db: AsyncIOMotorDatabase,
    redis: Redis | None,
    settings: Settings,
    character: CharacterDocument,
) -> tuple[list[esi.IndustryJobEntry], bool]:
    personal = await get_character_industry_jobs(
        db, redis, settings, character.access_token, character.character_id
    )
    return await _merge_corp(
        db,
        redis,
        settings,
        character,
        personal,
        lambda token, corp_id: get_corporation_industry_jobs(db, redis, settings, token, corp_id),
    )


@dataclass(frozen=True)
class CachedDataSource:
    label: str
    count: int
    cached_at: datetime | None
    shared: bool


async def data_summary(
    db: AsyncIOMotorDatabase, character: CharacterDocument
) -> list[CachedDataSource]:
    """Read-only inventory of what eve-build has cached for this character - no ESI
    calls, so the settings page stays fast and side-effect-free on GET."""
    sources = []
    for source in _PERSONAL_SOURCES:
        sources.append((source, {"character_id": character.character_id}))
    if corp_data_connected(character):
        corporation_id = cast(int, character.corporation_id)
        for source in _CORP_SOURCES:
            sources.append((source, {"corporation_id": corporation_id}))

    summaries = []
    for source, query in sources:
        count = await db[source.collection_name].count_documents(query)
        latest = await db[source.collection_name].find_one(query, sort=[("cached_at", -1)])
        summaries.append(
            CachedDataSource(
                label=source.label,
                count=count,
                cached_at=latest["cached_at"] if latest else None,
                shared=source in _CORP_SOURCES,
            )
        )
    return summaries


async def refresh_character_data(
    db: AsyncIOMotorDatabase, redis: Redis | None, character: CharacterDocument
) -> None:
    """Invalidates cached data so the next page view re-fetches fresh data from ESI."""
    for source in _PERSONAL_SOURCES:
        await esi_cache.invalidate_character_list(
            db,
            redis,
            collection_name=source.collection_name,
            cache_key_prefix=source.cache_key_prefix,
            character_id=character.character_id,
        )
    if corp_data_connected(character):
        for source in _CORP_SOURCES:
            await esi_cache.invalidate_corporation_list(
                db,
                redis,
                collection_name=source.collection_name,
                cache_key_prefix=source.cache_key_prefix,
                corporation_id=cast(int, character.corporation_id),
            )


async def clear_character_data(
    db: AsyncIOMotorDatabase, redis: Redis | None, character: CharacterDocument
) -> None:
    """Deletes everything eve-build has stored about this character: cached
    personal data (never the corp-shared collections - other connected
    characters in the same corp still rely on those) and the character
    document itself, including its stored access/refresh tokens."""
    for source in _PERSONAL_SOURCES:
        await esi_cache.invalidate_character_list(
            db,
            redis,
            collection_name=source.collection_name,
            cache_key_prefix=source.cache_key_prefix,
            character_id=character.character_id,
        )
    await db.characters.delete_one({"_id": character.character_id})
