import re

from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase
from redis.asyncio import Redis

from app.core.config import Settings
from app.services import cache


def cache_key(prefix: str, value: int) -> str:
    return f"{prefix}:{value}"


async def cached_docs_by_id(
    collection: AsyncIOMotorCollection,
    redis: Redis | None,
    settings: Settings,
    key_prefix: str,
    ids: set[int],
) -> dict[int, dict[str, object]]:
    cache_keys = {doc_id: cache_key(key_prefix, doc_id) for doc_id in ids}
    cached = await cache.get_many_cached(redis, list(cache_keys.values()))
    found: dict[int, dict[str, object]] = {
        doc_id: cached[key] for doc_id, key in cache_keys.items() if key in cached
    }

    missing_ids = ids - found.keys()
    if missing_ids:
        docs = await collection.find({"_id": {"$in": list(missing_ids)}}).to_list(None)
        for doc in docs:
            found[doc["_id"]] = doc
        await cache.set_many_cached(
            redis,
            {cache_keys[doc["_id"]]: doc for doc in docs},
            settings.redis_cache_ttl_seconds,
        )

    return found


async def type_docs(
    db: AsyncIOMotorDatabase,
    redis: Redis | None,
    settings: Settings,
    type_ids: set[int],
) -> dict[int, dict[str, object]]:
    return await cached_docs_by_id(db.sde_types, redis, settings, "sde_type", type_ids)


async def blueprint_docs(
    db: AsyncIOMotorDatabase,
    redis: Redis | None,
    settings: Settings,
    type_ids: set[int],
) -> dict[int, dict[str, object]]:
    return await cached_docs_by_id(db.sde_blueprints, redis, settings, "sde_blueprint", type_ids)


async def category_docs(
    db: AsyncIOMotorDatabase,
    redis: Redis | None,
    settings: Settings,
    category_ids: set[int],
) -> dict[int, dict[str, object]]:
    return await cached_docs_by_id(db.sde_categories, redis, settings, "sde_category", category_ids)


async def list_all_planet_schematics(db: AsyncIOMotorDatabase) -> list[dict[str, object]]:
    return await db.sde_planet_schematics.find({}).to_list(None)


async def search_items_by_name(
    db: AsyncIOMotorDatabase, query: str, limit: int = 50
) -> list[dict[str, object]]:
    """Search every published item in the game by name - not just blueprints, so a user can
    look up a target item (e.g. "Rifter") rather than its blueprint ("Rifter Blueprint")."""
    cursor = (
        db.sde_types.find(
            {"name": {"$regex": re.escape(query), "$options": "i"}, "published": True}
        )
        .sort("name", 1)
        .limit(limit)
    )
    return await cursor.to_list(None)


async def blueprint_for_product(
    db: AsyncIOMotorDatabase, product_type_id: int
) -> dict[str, object] | None:
    """Reverse lookup: which blueprint (or reaction formula) produces this item, if any.
    Backed by an index on product_type_id (see migration 0010)."""
    return await db.sde_blueprints.find_one({"product_type_id": product_type_id})


async def planet_schematic_for_product(
    db: AsyncIOMotorDatabase, product_type_id: int
) -> dict[str, object] | None:
    """Reverse lookup: which planetary schematic produces this item, if any. P0 raw materials
    (extracted, not manufactured) have none - unindexed since sde_planet_schematics is tiny
    (~90 docs total, same as list_all_planet_schematics scanning the whole collection)."""
    return await db.sde_planet_schematics.find_one({"output.type_id": product_type_id})


async def search_blueprints_by_name(
    db: AsyncIOMotorDatabase, query: str, limit: int = 50
) -> list[dict[str, object]]:
    """Search every blueprint in the game (not just ones a character owns) by product/blueprint
    name. Joins sde_blueprints -> sde_types in one aggregation rather than fetching the full
    ~8000-blueprint catalog into Python, since sde_blueprints docs don't carry a name of their
    own."""
    pipeline: list[dict[str, object]] = [
        {
            "$lookup": {
                "from": "sde_types",
                "localField": "_id",
                "foreignField": "_id",
                "as": "type",
            }
        },
        {"$unwind": "$type"},
        {
            "$match": {
                "type.name": {"$regex": re.escape(query), "$options": "i"},
                "type.published": True,
            }
        },
        {"$sort": {"type.name": 1}},
        {"$limit": limit},
        {
            "$project": {
                "_id": 1,
                "materials": 1,
                "product_type_id": 1,
                "product_quantity": 1,
                "manufacturing_time_seconds": 1,
                "activity_id": 1,
                "name": "$type.name",
                "tech_level": "$type.tech_level",
            }
        },
    ]
    return await db.sde_blueprints.aggregate(pipeline).to_list(None)
