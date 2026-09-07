import re
from datetime import UTC, datetime
from typing import cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from motor.motor_asyncio import AsyncIOMotorDatabase
from redis.asyncio import Redis

from app.core.config import Settings, get_settings
from app.db.mongo import get_database
from app.db.redis import get_redis
from app.deps import get_current_character
from app.models.character import CharacterDocument
from app.services import build_chain, plan, sde
from app.templating import templates
from app.web import format_isk, item_icon_url

router = APIRouter(prefix="/plans", tags=["plans"])

_PLAN_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_LIST_STYLE = ["/static/card.css", "/static/build.css"]
_DETAIL_STYLE = ["/static/card.css", "/static/build-detail.css"]


def _format_timestamp(value: datetime) -> str:
    return value.replace(tzinfo=UTC).strftime("%Y-%m-%d %H:%M UTC")


def _material_view(material: build_chain.RawMaterial) -> dict[str, object]:
    return {
        "icon_url": item_icon_url(material.type_id),
        "name": material.name,
        "quantity": material.quantity,
        "value": format_isk(material.quantity * material.unit_price),
    }


@router.get("", response_class=HTMLResponse)
async def list_plans(
    request: Request,
    character: CharacterDocument = Depends(get_current_character),
    db: AsyncIOMotorDatabase = Depends(get_database),
    redis: Redis | None = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    plans = await plan.list_plans(db, character.character_id)

    if not plans:
        return templates.TemplateResponse(
            request,
            "plans/list.html",
            {"character": character, "extra_stylesheets": _LIST_STYLE, "plans": []},
        )

    target_type_ids = {
        cast(int, cast(list[dict[str, object]], doc["jobs"])[0]["target_type_id"]) for doc in plans
    }
    type_docs = await sde.type_docs(db, redis, settings, target_type_ids)

    def _name(type_id: int) -> str:
        return str(type_docs.get(type_id, {}).get("name", f"Type {type_id}"))

    plans_view = []
    for doc in plans:
        jobs = cast(list[dict[str, object]], doc["jobs"])
        first_job_type_id = cast(int, jobs[0]["target_type_id"])
        jobs_text = "1 job" if len(jobs) == 1 else f"{len(jobs)} jobs"
        plans_view.append(
            {
                "plan_id": doc["_id"],
                "icon_url": item_icon_url(first_job_type_id),
                "name": _name(first_job_type_id),
                "jobs_text": jobs_text,
                "created_at": _format_timestamp(cast(datetime, doc["created_at"])),
            }
        )

    return templates.TemplateResponse(
        request,
        "plans/list.html",
        {"character": character, "extra_stylesheets": _LIST_STYLE, "plans": plans_view},
    )


@router.get("/create")
async def create_plan_from_build(
    character: CharacterDocument = Depends(get_current_character),
    db: AsyncIOMotorDatabase = Depends(get_database),
    type_id: int = Query(...),
    qty: int = Query(default=1, ge=1),
    build: str = Query(default=""),
) -> RedirectResponse:
    build_set = frozenset(int(t) for t in build.split(",") if t.strip().isdigit())
    plan_id = await plan.create_plan(db, character.character_id, type_id, qty, build_set)
    return RedirectResponse(f"/plans/{plan_id}")


@router.get("/{plan_id}/add-job")
async def add_job_to_plan(
    plan_id: str,
    character: CharacterDocument = Depends(get_current_character),
    db: AsyncIOMotorDatabase = Depends(get_database),
    type_id: int = Query(...),
    qty: int = Query(default=1, ge=1),
    build: str = Query(default=""),
) -> RedirectResponse:
    if not _PLAN_ID_RE.fullmatch(plan_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid plan id")
    build_set = frozenset(int(t) for t in build.split(",") if t.strip().isdigit())
    job_id = await plan.add_job(db, plan_id, character.character_id, type_id, qty, build_set)
    if job_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan not found")
    return RedirectResponse(f"/plans/{plan_id}")


@router.get("/{plan_id}/jobs/{job_id}/update")
async def update_job_quantity(
    plan_id: str,
    job_id: str,
    character: CharacterDocument = Depends(get_current_character),
    db: AsyncIOMotorDatabase = Depends(get_database),
    qty: int = Query(..., ge=1),
) -> RedirectResponse:
    if not _PLAN_ID_RE.fullmatch(plan_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid plan id")
    if not _PLAN_ID_RE.fullmatch(job_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid job id")
    safe_plan_id = plan_id
    updated = await plan.update_job_quantity(db, safe_plan_id, character.character_id, job_id, qty)
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan or job not found")
    return RedirectResponse(f"/plans/{safe_plan_id}")


@router.get("/{plan_id}/jobs/{job_id}/delete")
async def remove_job_from_plan(
    plan_id: str,
    job_id: str,
    character: CharacterDocument = Depends(get_current_character),
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> RedirectResponse:
    if not _PLAN_ID_RE.fullmatch(plan_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid plan id")
    removed = await plan.remove_job(db, plan_id, character.character_id, job_id)
    if removed is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan not found")
    if removed is False:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Job not found, or it's the plan's only job"
        )
    return RedirectResponse(f"/plans/{plan_id}")


@router.get("/{plan_id}", response_class=HTMLResponse)
async def plan_detail(
    request: Request,
    plan_id: str,
    character: CharacterDocument = Depends(get_current_character),
    db: AsyncIOMotorDatabase = Depends(get_database),
    redis: Redis | None = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    doc = await plan.get_plan(db, plan_id, character.character_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan not found")

    jobs = cast(list[dict[str, object]], doc["jobs"])
    resolutions = [
        await build_chain.resolve_build_chain(
            db,
            redis,
            settings,
            cast(int, job["target_type_id"]),
            cast(int, job["target_quantity"]),
            frozenset(cast(list[int], job["build_set"])),
        )
        for job in jobs
    ]

    total_cost = sum(resolution.raw_material_cost for resolution in resolutions)
    total_value = sum(resolution.output_value for resolution in resolutions)
    stats = {
        "total_cost": format_isk(total_cost),
        "total_value": format_isk(total_value),
        "total_profit": format_isk(total_value - total_cost),
        "job_count": str(len(resolutions)),
    }

    jobs_view = []
    for job, resolution in zip(jobs, resolutions, strict=True):
        job_profit = resolution.output_value - resolution.raw_material_cost
        jobs_view.append(
            {
                "job_id": job["job_id"],
                "icon_url": item_icon_url(resolution.target_type_id),
                "target_name": resolution.target_name,
                "target_quantity": resolution.target_quantity,
                "delete_enabled": len(jobs) > 1,
                "cost": format_isk(resolution.raw_material_cost),
                "profit": format_isk(job_profit),
                "materials": [_material_view(m) for m in resolution.raw_materials],
            }
        )

    combined_materials = build_chain.aggregate_raw_materials(resolutions)

    return templates.TemplateResponse(
        request,
        "plans/detail.html",
        {
            "character": character,
            "extra_stylesheets": _DETAIL_STYLE,
            "plan_id": plan_id,
            "created_at": _format_timestamp(cast(datetime, doc["created_at"])),
            "stats": stats,
            "jobs": jobs_view,
            "combined_materials": [_material_view(m) for m in combined_materials],
        },
    )
