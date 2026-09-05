import uuid
from datetime import UTC, datetime

from motor.motor_asyncio import AsyncIOMotorDatabase


def _job_doc(
    target_type_id: int, target_quantity: int, build_set: frozenset[int]
) -> dict[str, object]:
    return {
        "job_id": str(uuid.uuid4()),
        "target_type_id": target_type_id,
        "target_quantity": target_quantity,
        "build_set": sorted(build_set),
    }


async def create_plan(
    db: AsyncIOMotorDatabase,
    character_id: int,
    target_type_id: int,
    target_quantity: int,
    build_set: frozenset[int],
) -> str:
    """Saves a plan with a single initial job - just enough to re-derive that job's
    BuildResolution later via resolve_build_chain, not a frozen snapshot of costs/materials,
    which would go stale as prices change. Returns the new plan's id. Further jobs can be
    appended later via add_job."""
    plan_id = str(uuid.uuid4())
    now = datetime.now(UTC).replace(tzinfo=None)
    await db.plans.insert_one(
        {
            "_id": plan_id,
            "character_id": character_id,
            "jobs": [_job_doc(target_type_id, target_quantity, build_set)],
            "created_at": now,
            "updated_at": now,
        }
    )
    return plan_id


async def add_job(
    db: AsyncIOMotorDatabase,
    plan_id: str,
    character_id: int,
    target_type_id: int,
    target_quantity: int,
    build_set: frozenset[int],
) -> str | None:
    """Appends a job to an existing plan. Returns the new job's id, or None if the plan
    doesn't exist or isn't owned by this character (the route turns that into a 404)."""
    job = _job_doc(target_type_id, target_quantity, build_set)
    now = datetime.now(UTC).replace(tzinfo=None)
    result = await db.plans.update_one(
        {"_id": plan_id, "character_id": character_id},
        {"$push": {"jobs": job}, "$set": {"updated_at": now}},
    )
    if result.matched_count == 0:
        return None
    return str(job["job_id"])


async def get_plan(
    db: AsyncIOMotorDatabase, plan_id: str, character_id: int
) -> dict[str, object] | None:
    return await db.plans.find_one({"_id": plan_id, "character_id": character_id})


async def list_plans(db: AsyncIOMotorDatabase, character_id: int) -> list[dict[str, object]]:
    cursor = db.plans.find({"character_id": character_id}).sort("updated_at", -1)
    return await cursor.to_list(None)
