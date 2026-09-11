import uuid
from datetime import UTC, datetime
from typing import cast

from motor.motor_asyncio import AsyncIOMotorDatabase


def _job_doc(
    target_type_id: int,
    target_quantity: int,
    build_set: frozenset[int],
    blueprint_item_id: int | None = None,
) -> dict[str, object]:
    return {
        "job_id": str(uuid.uuid4()),
        "target_type_id": target_type_id,
        "target_quantity": target_quantity,
        "build_set": sorted(build_set),
        "blueprint_item_id": blueprint_item_id,
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
            "name": "",
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
    blueprint_item_id: int | None = None,
) -> str | None:
    """Appends a job to an existing plan. blueprint_item_id records which owned blueprint
    (if any) the job was added from, so the plan page can link back to it. Returns the new
    job's id, or None if the plan doesn't exist or isn't owned by this character (the route
    turns that into a 404)."""
    job = _job_doc(target_type_id, target_quantity, build_set, blueprint_item_id)
    now = datetime.now(UTC).replace(tzinfo=None)
    result = await db.plans.update_one(
        {"_id": plan_id, "character_id": character_id},
        {"$push": {"jobs": job}, "$set": {"updated_at": now}},
    )
    if result.matched_count == 0:
        return None
    return str(job["job_id"])


async def update_job_quantity(
    db: AsyncIOMotorDatabase,
    plan_id: str,
    character_id: int,
    job_id: str,
    target_quantity: int,
) -> bool:
    """Updates one job's desired output quantity in place - the rest of the job
    (target_type_id, build_set) is unchanged, so its BuildResolution just re-scales next
    time the plan is viewed. Returns False if the plan/job doesn't exist or isn't owned by
    this character (the route turns that into a 404)."""
    now = datetime.now(UTC).replace(tzinfo=None)
    result = await db.plans.update_one(
        {"_id": plan_id, "character_id": character_id, "jobs.job_id": job_id},
        {"$set": {"jobs.$.target_quantity": target_quantity, "updated_at": now}},
    )
    return result.matched_count > 0


async def update_job_build_flag(
    db: AsyncIOMotorDatabase,
    plan_id: str,
    character_id: int,
    job_id: str,
    type_id: int,
    build: bool,
) -> bool:
    """Adds or removes one material from a job's build_set in place - the rest of the job
    (target_type_id, target_quantity) is unchanged, so its BuildResolution just
    re-expands/re-collapses that branch next time the plan is viewed. Returns False if the
    plan/job doesn't exist or isn't owned by this character (the route turns that into a
    404)."""
    doc = await db.plans.find_one({"_id": plan_id, "character_id": character_id})
    if doc is None:
        return False
    jobs = cast(list[dict[str, object]], doc["jobs"])
    job = next((j for j in jobs if j["job_id"] == job_id), None)
    if job is None:
        return False
    build_set = set(cast(list[int], job["build_set"]))
    if build:
        build_set.add(type_id)
    else:
        build_set.discard(type_id)
    now = datetime.now(UTC).replace(tzinfo=None)
    result = await db.plans.update_one(
        {"_id": plan_id, "character_id": character_id, "jobs.job_id": job_id},
        {"$set": {"jobs.$.build_set": sorted(build_set), "updated_at": now}},
    )
    return result.matched_count > 0


async def set_build_flag_for_all_jobs(
    db: AsyncIOMotorDatabase, plan_id: str, character_id: int, type_id: int, build: bool
) -> bool:
    """Adds or removes one material from every job's build_set in the plan at once - the
    combined Bill of Materials page's per-material Build/Buy toggle affects the whole plan,
    unlike a single job's own toggle. Returns False if the plan doesn't exist or isn't owned
    by this character (the route turns that into a 404)."""
    doc = await db.plans.find_one({"_id": plan_id, "character_id": character_id})
    if doc is None:
        return False
    jobs = cast(list[dict[str, object]], doc["jobs"])
    for job in jobs:
        build_set = set(cast(list[int], job["build_set"]))
        if build:
            build_set.add(type_id)
        else:
            build_set.discard(type_id)
        job["build_set"] = sorted(build_set)
    now = datetime.now(UTC).replace(tzinfo=None)
    await db.plans.update_one(
        {"_id": plan_id, "character_id": character_id},
        {"$set": {"jobs": jobs, "updated_at": now}},
    )
    return True


async def remove_job(
    db: AsyncIOMotorDatabase, plan_id: str, character_id: int, job_id: str
) -> bool | None:
    """Removes one job from a plan. Returns None if the plan doesn't exist or isn't owned
    by this character (the route turns that into a 404), False if the job doesn't exist or
    is the plan's last remaining job - a plan always needs at least one job, so delete the
    whole plan instead (the route turns that into a 400) - or True once removed."""
    doc = await db.plans.find_one({"_id": plan_id, "character_id": character_id})
    if doc is None:
        return None
    jobs = cast(list[dict[str, object]], doc["jobs"])
    if len(jobs) <= 1 or not any(job["job_id"] == job_id for job in jobs):
        return False
    now = datetime.now(UTC).replace(tzinfo=None)
    await db.plans.update_one(
        {"_id": plan_id, "character_id": character_id},
        {"$pull": {"jobs": {"job_id": job_id}}, "$set": {"updated_at": now}},
    )
    return True


async def rename_plan(db: AsyncIOMotorDatabase, plan_id: str, character_id: int, name: str) -> bool:
    """Sets a plan's display name. Returns False if the plan doesn't exist or isn't owned
    by this character (the route turns that into a 404)."""
    now = datetime.now(UTC).replace(tzinfo=None)
    result = await db.plans.update_one(
        {"_id": plan_id, "character_id": character_id},
        {"$set": {"name": name, "updated_at": now}},
    )
    return result.matched_count > 0


async def delete_plan(db: AsyncIOMotorDatabase, plan_id: str, character_id: int) -> bool:
    """Deletes a whole plan, including all its jobs. Returns False if the plan doesn't exist
    or isn't owned by this character (the route turns that into a 404)."""
    result = await db.plans.delete_one({"_id": plan_id, "character_id": character_id})
    return result.deleted_count > 0


async def get_plan(
    db: AsyncIOMotorDatabase, plan_id: str, character_id: int
) -> dict[str, object] | None:
    return await db.plans.find_one({"_id": plan_id, "character_id": character_id})


async def list_plans(db: AsyncIOMotorDatabase, character_id: int) -> list[dict[str, object]]:
    cursor = db.plans.find({"character_id": character_id}).sort("updated_at", -1)
    return await cursor.to_list(None)
