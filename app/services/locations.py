from dataclasses import dataclass
from datetime import UTC, datetime

from motor.motor_asyncio import AsyncIOMotorDatabase
from redis.asyncio import Redis

from app.core.config import Settings
from app.services import cache, esi, sde


def resolve_container_chain(location_id: int, assets_by_item_id: dict[int, esi.AssetEntry]) -> int:
    """An asset's location_id can be another item's item_id if it's sitting inside a
    container (which can itself be inside another container). Walk that chain using the
    already-fetched asset list until reaching a real station/structure id, so we never try
    to resolve a container's item_id as if it were a station or structure."""
    current = location_id
    visited: set[int] = set()
    while current in assets_by_item_id and current not in visited:
        visited.add(current)
        asset = assets_by_item_id[current]
        if asset.location_type != "item":
            return asset.location_id
        current = asset.location_id
    return current


@dataclass(frozen=True)
class LocationInfo:
    name: str | None
    security_status: float | None
    type_id: int | None = None


async def _resolve_location_system_ids(
    db: AsyncIOMotorDatabase,
    redis: Redis | None,
    settings: Settings,
    access_token: str,
    location_ids: set[int],
) -> dict[int, tuple[str | None, int | None, int | None]]:
    cache_keys = {
        location_id: sde.cache_key("location_details", location_id) for location_id in location_ids
    }
    cached = await cache.get_many_cached(redis, list(cache_keys.values()))
    resolved: dict[int, tuple[str | None, int | None, int | None]] = {
        location_id: (cached[key]["name"], cached[key]["system_id"], cached[key].get("type_id"))
        for location_id, key in cache_keys.items()
        if key in cached
    }

    remaining = location_ids - resolved.keys()
    if remaining:
        mongo_docs = await db.location_names.find(
            {"_id": {"$in": list(remaining)}, "name": {"$ne": None}}
        ).to_list(None)
        newly_resolved = {
            doc["_id"]: (doc["name"], doc.get("system_id"), doc.get("type_id"))
            for doc in mongo_docs
        }
        resolved.update(newly_resolved)
        await cache.set_many_cached(
            redis,
            {
                cache_keys[loc_id]: {"name": name, "system_id": system_id, "type_id": type_id}
                for loc_id, (name, system_id, type_id) in newly_resolved.items()
            },
            settings.redis_cache_ttl_seconds,
        )

    for location_id in location_ids - resolved.keys():
        details = await esi.get_location_details(settings, access_token, location_id)
        resolved[location_id] = (details.name, details.system_id, details.type_id)
        # Only persist successful resolutions - a failed lookup (e.g. missing scope or no
        # docking access) should be retried next time rather than cached as a permanent None.
        if details.name is not None:
            await db.location_names.update_one(
                {"_id": location_id},
                {
                    "$set": {
                        "name": details.name,
                        "system_id": details.system_id,
                        "type_id": details.type_id,
                        "cached_at": datetime.now(UTC).replace(tzinfo=None),
                    }
                },
                upsert=True,
            )
            await cache.set_many_cached(
                redis,
                {
                    cache_keys[location_id]: {
                        "name": details.name,
                        "system_id": details.system_id,
                        "type_id": details.type_id,
                    }
                },
                settings.redis_cache_ttl_seconds,
            )

    return resolved


async def _resolve_system_security_statuses(
    db: AsyncIOMotorDatabase,
    redis: Redis | None,
    settings: Settings,
    system_ids: set[int],
) -> dict[int, float | None]:
    cache_keys = {
        system_id: sde.cache_key("system_security", system_id) for system_id in system_ids
    }
    cached = await cache.get_many_cached(redis, list(cache_keys.values()))
    resolved: dict[int, float | None] = {
        system_id: cached[key] for system_id, key in cache_keys.items() if key in cached
    }

    remaining = system_ids - resolved.keys()
    if remaining:
        mongo_docs = await db.system_security.find({"_id": {"$in": list(remaining)}}).to_list(None)
        newly_resolved = {doc["_id"]: doc["security_status"] for doc in mongo_docs}
        resolved.update(newly_resolved)
        await cache.set_many_cached(
            redis,
            {cache_keys[sys_id]: status for sys_id, status in newly_resolved.items()},
            settings.redis_cache_ttl_seconds,
        )

    # Security status never changes, so unlike location names, a lookup that fails to resolve
    # here is not retried - it's just left absent from the result.
    for system_id in system_ids - resolved.keys():
        status = await esi.get_system_security_status(settings, system_id)
        resolved[system_id] = status
        if status is not None:
            await db.system_security.update_one(
                {"_id": system_id}, {"$set": {"security_status": status}}, upsert=True
            )
            await cache.set_many_cached(
                redis, {cache_keys[system_id]: status}, settings.redis_cache_ttl_seconds
            )

    return resolved


async def resolve_planet_names(
    db: AsyncIOMotorDatabase,
    redis: Redis | None,
    settings: Settings,
    planet_ids: set[int],
) -> dict[int, str | None]:
    """Planet names never change, so - like system security status - a resolved name is
    cached forever in Mongo rather than re-fetched every TTL window."""
    cache_keys = {planet_id: sde.cache_key("planet_name", planet_id) for planet_id in planet_ids}
    cached = await cache.get_many_cached(redis, list(cache_keys.values()))
    resolved: dict[int, str | None] = {
        planet_id: cached[key] for planet_id, key in cache_keys.items() if key in cached
    }

    remaining = planet_ids - resolved.keys()
    if remaining:
        mongo_docs = await db.planet_names.find({"_id": {"$in": list(remaining)}}).to_list(None)
        newly_resolved = {doc["_id"]: doc["name"] for doc in mongo_docs}
        resolved.update(newly_resolved)
        await cache.set_many_cached(
            redis,
            {cache_keys[planet_id]: name for planet_id, name in newly_resolved.items()},
            settings.redis_cache_ttl_seconds,
        )

    for planet_id in planet_ids - resolved.keys():
        name = await esi.get_planet_name(settings, planet_id)
        resolved[planet_id] = name
        if name is not None:
            await db.planet_names.update_one(
                {"_id": planet_id},
                {"$set": {"name": name, "cached_at": datetime.now(UTC).replace(tzinfo=None)}},
                upsert=True,
            )
            await cache.set_many_cached(
                redis, {cache_keys[planet_id]: name}, settings.redis_cache_ttl_seconds
            )

    return resolved


async def resolve_system_names(
    db: AsyncIOMotorDatabase,
    redis: Redis | None,
    settings: Settings,
    system_ids: set[int],
) -> dict[int, str | None]:
    cache_keys = {system_id: sde.cache_key("system_name", system_id) for system_id in system_ids}
    cached = await cache.get_many_cached(redis, list(cache_keys.values()))
    resolved: dict[int, str | None] = {
        system_id: cached[key] for system_id, key in cache_keys.items() if key in cached
    }

    remaining = system_ids - resolved.keys()
    if remaining:
        mongo_docs = await db.system_names.find({"_id": {"$in": list(remaining)}}).to_list(None)
        newly_resolved = {doc["_id"]: doc["name"] for doc in mongo_docs}
        resolved.update(newly_resolved)
        await cache.set_many_cached(
            redis,
            {cache_keys[system_id]: name for system_id, name in newly_resolved.items()},
            settings.redis_cache_ttl_seconds,
        )

    for system_id in system_ids - resolved.keys():
        name = await esi.get_system_name(settings, system_id)
        resolved[system_id] = name
        if name is not None:
            await db.system_names.update_one(
                {"_id": system_id},
                {"$set": {"name": name, "cached_at": datetime.now(UTC).replace(tzinfo=None)}},
                upsert=True,
            )
            await cache.set_many_cached(
                redis, {cache_keys[system_id]: name}, settings.redis_cache_ttl_seconds
            )

    return resolved


async def resolve_location_info(
    db: AsyncIOMotorDatabase,
    redis: Redis | None,
    settings: Settings,
    access_token: str,
    location_ids: set[int],
) -> dict[int, LocationInfo]:
    name_and_system_by_location = await _resolve_location_system_ids(
        db, redis, settings, access_token, location_ids
    )
    system_ids = {
        system_id
        for _, system_id, _ in name_and_system_by_location.values()
        if system_id is not None
    }
    security_by_system = await _resolve_system_security_statuses(db, redis, settings, system_ids)

    return {
        location_id: LocationInfo(
            name=name,
            security_status=security_by_system.get(system_id) if system_id is not None else None,
            type_id=type_id,
        )
        for location_id, (name, system_id, type_id) in name_and_system_by_location.items()
    }
