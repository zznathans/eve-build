from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import UpdateMany

from app.core.config import Settings

MIGRATION_ID = "0003_add_volume_to_sde_types"

# EVE's invTypes has ~53k rows but only a few hundred distinct volume values (e.g. every
# type_id sharing volume=0.01 collapses into a single UpdateMany) - grouping by value first
# turns tens of thousands of per-document update_one calls into a couple hundred bulk ops.
_BULK_CHUNK_SIZE = 1000


async def apply(db: AsyncIOMotorDatabase, settings: Settings) -> None:
    volume_by_type_id: dict[int, float] = {}
    async for row in db["invTypes"].find({}, {"typeID": 1, "volume": 1}):
        volume_by_type_id[row["typeID"]] = row.get("volume") or 0.0

    type_ids_by_volume: dict[float, list[int]] = {}
    for type_id, volume in volume_by_type_id.items():
        type_ids_by_volume.setdefault(volume, []).append(type_id)

    operations = [
        UpdateMany({"_id": {"$in": type_ids}}, {"$set": {"volume": volume}})
        for volume, type_ids in type_ids_by_volume.items()
    ]

    for start in range(0, len(operations), _BULK_CHUNK_SIZE):
        chunk = operations[start : start + _BULK_CHUNK_SIZE]
        if chunk:
            await db["sde_types"].bulk_write(chunk, ordered=False)
