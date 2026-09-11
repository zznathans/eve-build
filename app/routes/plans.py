import re
from datetime import UTC, datetime
from html import escape
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
from app.services import build_chain, character_data, plan, sde
from app.templating import templates
from app.web import format_isk, format_number, gauge_cell_html, item_icon_url

router = APIRouter(prefix="/plans", tags=["plans"])

_PLAN_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_LIST_STYLE = ["/static/card.css", "/static/build.css"]
_DETAIL_STYLE = ["/static/card.css", "/static/build-detail.css"]


def _format_timestamp(value: datetime) -> str:
    return value.replace(tzinfo=UTC).strftime("%Y-%m-%d %H:%M UTC")


def _material_view(
    material: build_chain.RawMaterial, owned_by_type_id: dict[int, int]
) -> dict[str, object]:
    owned = owned_by_type_id.get(material.type_id, 0)
    percentage = 100.0 if material.quantity <= 0 else min(100.0, owned / material.quantity * 100)
    return {
        "icon_url": item_icon_url(material.type_id),
        "name": material.name,
        "quantity": material.quantity,
        "value": format_isk(material.quantity * material.unit_price),
        "availability_html": gauge_cell_html(
            percentage,
            format_number(material.quantity),
            owned_text=format_number(owned),
        ),
    }


def _job_flag_context(
    resolution: build_chain.BuildResolution,
    *,
    plan_id: str,
    job_id: str,
    pi_type_ids: frozenset[int],
) -> dict[str, list[dict[str, object]]]:
    """Materials + build steps for one job on the plan detail page, with clickable
    Build/Buy toggle links that edit the job's stored build_set in place (mirrors
    app/routes/build.py's _build_resolution_context, but persists to the plan instead of
    round-tripping through query-string state). Planetary materials are split into their
    own list so the page can show them as a separate category from other raw materials."""

    def _toggle_href(type_id: int, *, build: bool) -> str:
        flag = "true" if build else "false"
        return f"/plans/{plan_id}/jobs/{job_id}/build-set?type_id={type_id}&build={flag}"

    def _buy_flag(step_type_id: int) -> str:
        if step_type_id == resolution.target_type_id:
            return ""
        href = escape(_toggle_href(step_type_id, build=False))
        return f'<a class="flag flag-buy-toggle" href="{href}">Buy</a>'

    steps = [
        {
            "icon_url": item_icon_url(step.type_id),
            "name": step.name,
            "buy_flag_html": _buy_flag(step.type_id),
            "runs": step.runs,
            "quantity_needed": step.quantity_needed,
        }
        for step in resolution.steps
    ]

    def _material_flag(material: build_chain.RawMaterial) -> str:
        if not material.is_buildable:
            return '<span class="flag flag-buy">Bought</span>'
        href = escape(_toggle_href(material.type_id, build=True))
        return f'<a class="flag flag-build" href="{href}">Build</a>'

    materials: list[dict[str, object]] = []
    pi_materials: list[dict[str, object]] = []
    for material in resolution.raw_materials:
        view = {
            "icon_url": item_icon_url(material.type_id),
            "name": material.name,
            "quantity": material.quantity,
            "value": format_isk(material.quantity * material.unit_price),
            "flag_html": _material_flag(material),
        }
        if material.type_id in pi_type_ids:
            pi_materials.append(view)
        else:
            materials.append(view)

    return {"materials": materials, "pi_materials": pi_materials, "steps": steps}


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

    all_target_type_ids = {
        cast(int, job["target_type_id"])
        for doc in plans
        for job in cast(list[dict[str, object]], doc["jobs"])
    }
    type_docs = await sde.type_docs(db, redis, settings, all_target_type_ids)

    def _name(type_id: int) -> str:
        return str(type_docs.get(type_id, {}).get("name", f"Type {type_id}"))

    plans_view = []
    for doc in plans:
        jobs = cast(list[dict[str, object]], doc["jobs"])
        jobs_text = "1 job" if len(jobs) == 1 else f"{len(jobs)} jobs"
        plans_view.append(
            {
                "plan_id": doc["_id"],
                "name": str(doc.get("name") or "") or "Untitled Plan",
                "jobs_text": jobs_text,
                "created_at": _format_timestamp(cast(datetime, doc["created_at"])),
                "job_icons": [
                    {
                        "icon_url": item_icon_url(cast(int, job["target_type_id"])),
                        "name": _name(cast(int, job["target_type_id"])),
                    }
                    for job in jobs
                ],
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


@router.get("/{plan_id}/jobs/{job_id}/build-set")
async def set_job_build_flag(
    plan_id: str,
    job_id: str,
    character: CharacterDocument = Depends(get_current_character),
    db: AsyncIOMotorDatabase = Depends(get_database),
    type_id: int = Query(...),
    build: bool = Query(...),
) -> RedirectResponse:
    if not _PLAN_ID_RE.fullmatch(plan_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid plan id")
    if not _PLAN_ID_RE.fullmatch(job_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid job id")
    updated = await plan.update_job_build_flag(
        db, plan_id, character.character_id, job_id, type_id, build
    )
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan or job not found")
    return RedirectResponse(f"/plans/{plan_id}")


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


@router.get("/{plan_id}/rename")
async def rename_plan(
    plan_id: str,
    character: CharacterDocument = Depends(get_current_character),
    db: AsyncIOMotorDatabase = Depends(get_database),
    name: str = Query(default="", max_length=100),
) -> RedirectResponse:
    if not _PLAN_ID_RE.fullmatch(plan_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid plan id")
    renamed = await plan.rename_plan(db, plan_id, character.character_id, name.strip())
    if not renamed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan not found")
    return RedirectResponse(f"/plans/{plan_id}")


@router.get("/{plan_id}/delete")
async def delete_plan(
    plan_id: str,
    character: CharacterDocument = Depends(get_current_character),
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> RedirectResponse:
    if not _PLAN_ID_RE.fullmatch(plan_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid plan id")
    deleted = await plan.delete_plan(db, plan_id, character.character_id)
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan not found")
    return RedirectResponse("/plans")


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

    combined_materials = build_chain.aggregate_raw_materials(resolutions)

    all_material_type_ids = {
        material.type_id for resolution in resolutions for material in resolution.raw_materials
    }
    type_docs = await sde.type_docs(db, redis, settings, all_material_type_ids)
    pi_type_ids = frozenset(
        type_id
        for type_id in all_material_type_ids
        if type_docs.get(type_id, {}).get("category_id") in sde.PLANETARY_MATERIAL_CATEGORY_IDS
    )

    jobs_view = []
    for job, resolution in zip(jobs, resolutions, strict=True):
        job_id = cast(str, job["job_id"])
        job_profit = resolution.output_value - resolution.raw_material_cost
        flag_context = _job_flag_context(
            resolution, plan_id=plan_id, job_id=job_id, pi_type_ids=pi_type_ids
        )
        jobs_view.append(
            {
                "job_id": job_id,
                "icon_url": item_icon_url(resolution.target_type_id),
                "target_name": resolution.target_name,
                "target_quantity": resolution.target_quantity,
                "delete_enabled": len(jobs) > 1,
                "cost": format_isk(resolution.raw_material_cost),
                "profit": format_isk(job_profit),
                "materials": flag_context["materials"],
                "pi_materials": flag_context["pi_materials"],
                "steps": flag_context["steps"],
            }
        )

    assets, _ = await character_data.get_merged_assets(db, redis, settings, character)
    owned_by_type_id: dict[int, int] = {}
    for asset in assets:
        owned_by_type_id[asset.type_id] = owned_by_type_id.get(asset.type_id, 0) + asset.quantity

    combined_materials_view = []
    combined_pi_materials_view = []
    for material in combined_materials:
        view = _material_view(material, owned_by_type_id)
        if material.type_id in pi_type_ids:
            combined_pi_materials_view.append(view)
        else:
            combined_materials_view.append(view)

    return templates.TemplateResponse(
        request,
        "plans/detail.html",
        {
            "character": character,
            "extra_stylesheets": _DETAIL_STYLE,
            "plan_id": plan_id,
            "name": str(doc.get("name") or ""),
            "created_at": _format_timestamp(cast(datetime, doc["created_at"])),
            "stats": stats,
            "jobs": jobs_view,
            "combined_materials": combined_materials_view,
            "combined_pi_materials": combined_pi_materials_view,
        },
    )
