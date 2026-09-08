import gzip
import json
from pathlib import Path
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import UpdateMany

from app.core.config import Settings

MIGRATION_ID = "0004_add_category_id_to_sde_types"

# Only a few dozen distinct category_id values exist across every group - grouping by value
# first turns tens of thousands of per-document update_one calls into a handful of bulk ops.
_BULK_CHUNK_SIZE = 1000


async def apply(db: AsyncIOMotorDatabase, settings: Settings) -> None:
    path = Path(settings.sde_data_dir) / "invGroups.json.gz"
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        groups: list[dict[str, Any]] = json.load(fh)
    category_id_by_group_id = {row["groupID"]: row["categoryID"] for row in groups}

    type_docs = await db["sde_types"].find({}, {"group_id": 1}).to_list(None)

    type_ids_by_category_id: dict[int, list[int]] = {}
    for doc in type_docs:
        group_id = doc.get("group_id")
        if group_id not in category_id_by_group_id:
            continue
        type_ids_by_category_id.setdefault(category_id_by_group_id[group_id], []).append(doc["_id"])

    operations = [
        UpdateMany({"_id": {"$in": type_ids}}, {"$set": {"category_id": category_id}})
        for category_id, type_ids in type_ids_by_category_id.items()
    ]

    for start in range(0, len(operations), _BULK_CHUNK_SIZE):
        chunk = operations[start : start + _BULK_CHUNK_SIZE]
        if chunk:
            await db["sde_types"].bulk_write(chunk, ordered=False)
