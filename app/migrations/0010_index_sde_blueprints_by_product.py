from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import Settings

MIGRATION_ID = "0010_index_sde_blueprints_by_product"


async def apply(db: AsyncIOMotorDatabase, settings: Settings) -> None:
    # No explicit name - Mongo auto-names this "product_type_id_1". The index_sync config
    # (app/config/mongo_indexes/sde_blueprints.json) manages this same index going forward and
    # must use that exact name, or index_sync's create_index (with a different explicit name)
    # collides with this one on every startup - same key pattern, different name, which Mongo
    # rejects with IndexOptionsConflict.
    await db["sde_blueprints"].create_index([("product_type_id", 1)])
