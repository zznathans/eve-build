from html import escape
from typing import cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from motor.motor_asyncio import AsyncIOMotorDatabase
from redis.asyncio import Redis

from app.core.config import Settings, get_settings
from app.db.mongo import get_database
from app.db.redis import get_redis
from app.deps import get_current_character_optional
from app.models.character import CharacterDocument
from app.services import build_chain, sde
from app.templating import templates
from app.web import format_isk, item_icon_url

router = APIRouter(prefix="/build", tags=["build"])

_CHOOSER_STYLE = ["/static/card.css", "/static/build.css"]
_LIST_STYLE = ["/static/card.css", "/static/build.css"]
_DETAIL_STYLE = ["/static/card.css", "/static/build-detail.css"]


def _build_toggle_href(
    target_type_id: int,
    qty: int,
    build_set: frozenset[int],
    toggled_type_id: int,
    plan_id: str | None,
    *,
    adding: bool,
) -> str:
    updated = (build_set | {toggled_type_id}) if adding else (build_set - {toggled_type_id})
    query = f"qty={qty}"
    if updated:
        query += f"&build={','.join(str(t) for t in sorted(updated))}"
    if plan_id:
        query += f"&plan_id={plan_id}"
    return f"/build/items/{target_type_id}?{query}"


def _build_resolution_context(
    resolution: build_chain.BuildResolution,
    *,
    type_id: int,
    qty: int,
    build_set: frozenset[int],
    plan_id: str | None,
) -> dict[str, list[dict[str, object]]]:
    """Data for the Materials + Build steps sections of the item build-chain page, with
    clickable Build/Buy toggle links (see _build_toggle_href)."""

    def _buy_flag(step_type_id: int) -> str:
        if step_type_id == type_id:
            return ""
        href = escape(
            _build_toggle_href(type_id, qty, build_set, step_type_id, plan_id, adding=False)
        )
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
        href = escape(
            _build_toggle_href(type_id, qty, build_set, material.type_id, plan_id, adding=True)
        )
        return f'<a class="flag flag-build" href="{href}">Build</a>'

    materials = [
        {
            "icon_url": item_icon_url(material.type_id),
            "name": material.name,
            "is_buildable": material.is_buildable,
            "flag_html": _material_flag(material),
            "quantity": material.quantity,
            "cost": format_isk(material.quantity * material.unit_price),
        }
        for material in resolution.raw_materials
    ]

    return {"materials": materials, "steps": steps}


@router.get("", response_class=HTMLResponse)
async def build_chooser(
    request: Request,
    character: CharacterDocument | None = Depends(get_current_character_optional),
    plan_id: str | None = Query(default=None),
) -> HTMLResponse:
    items_href = f"/build/items?plan_id={plan_id}" if plan_id else "/build/items"
    if character is None:
        blueprint_card_href = "/blueprints/catalog"
        blueprint_card_title = "I know which blueprint I want"
        blueprint_card_description = (
            "Search the blueprint catalog directly and see its materials, cost, and "
            "output for a single run."
        )
    else:
        blueprint_card_href = "/blueprints"
        blueprint_card_title = "Select from one of my existing blueprints"
        blueprint_card_description = (
            "Pick one of your own blueprints and see its materials, cost, and output "
            "for a single run."
        )
    return templates.TemplateResponse(
        request,
        "build/chooser.html",
        {
            "character": character,
            "extra_stylesheets": _CHOOSER_STYLE,
            "items_href": items_href,
            "blueprint_card_href": blueprint_card_href,
            "blueprint_card_title": blueprint_card_title,
            "blueprint_card_description": blueprint_card_description,
        },
    )


@router.get("/items", response_class=HTMLResponse)
async def item_search(
    request: Request,
    character: CharacterDocument | None = Depends(get_current_character_optional),
    db: AsyncIOMotorDatabase = Depends(get_database),
    q: str = Query(default=""),
    plan_id: str | None = Query(default=None),
) -> HTMLResponse:
    query = q.strip()

    items: list[dict[str, object]] = []
    if len(query) >= 2:
        docs = await sde.search_items_by_name(db, query)
        results_state = "results" if docs else "no_match"
        items = [
            {
                "type_id": doc["_id"],
                "icon_url": item_icon_url(cast(int, doc["_id"])),
                "name": str(doc["name"]),
            }
            for doc in docs
        ]
    elif query:
        results_state = "too_short"
    else:
        results_state = "empty"

    return templates.TemplateResponse(
        request,
        "build/item_search.html",
        {
            "character": character,
            "extra_stylesheets": _LIST_STYLE,
            "query": query,
            "plan_id": plan_id,
            "item_href_suffix": f"?plan_id={plan_id}" if plan_id else "",
            "results_state": results_state,
            "items": items,
        },
    )


@router.get("/items/{type_id}", response_class=HTMLResponse)
async def item_build_chain(
    request: Request,
    type_id: int,
    character: CharacterDocument | None = Depends(get_current_character_optional),
    db: AsyncIOMotorDatabase = Depends(get_database),
    redis: Redis | None = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    qty: int = Query(default=1, ge=1),
    build: str = Query(default=""),
    plan_id: str | None = Query(default=None),
) -> HTMLResponse:
    type_docs = await sde.type_docs(db, redis, settings, {type_id})
    type_doc = type_docs.get(type_id)
    if type_doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")

    build_set = frozenset(int(t) for t in build.split(",") if t.strip().isdigit())
    resolution = await build_chain.resolve_build_chain(db, redis, settings, type_id, qty, build_set)
    page_title = f"{resolution.target_name} - eve-build"

    context: dict[str, object] = {
        "character": character,
        "extra_stylesheets": _DETAIL_STYLE,
        "page_title": page_title,
        "item_icon_url": item_icon_url(type_id),
        "item_name": resolution.target_name,
        "qty": qty,
        "build": build,
        "plan_id": plan_id,
        "is_buildable": resolution.is_buildable,
    }

    if resolution.is_buildable:
        profit = resolution.output_value - resolution.raw_material_cost
        context["stats"] = {
            "cost": format_isk(resolution.raw_material_cost),
            "value": format_isk(resolution.output_value),
            "profit": format_isk(profit),
            "step_count": str(len(resolution.steps)),
        }
        context.update(
            _build_resolution_context(
                resolution, type_id=type_id, qty=qty, build_set=build_set, plan_id=plan_id
            )
        )
        add_to_plan_href = None
        if character is not None:
            if plan_id:
                add_to_plan_href = (
                    f"/plans/{plan_id}/add-job?type_id={type_id}&qty={qty}&build={build}"
                )
            else:
                add_to_plan_href = f"/plans/create?type_id={type_id}&qty={qty}&build={build}"
        context["add_to_plan_href"] = add_to_plan_href

    return templates.TemplateResponse(request, "build/item_build_chain.html", context)
