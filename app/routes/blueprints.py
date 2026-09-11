import math
from dataclasses import dataclass
from html import escape
from typing import Any, cast
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from motor.motor_asyncio import AsyncIOMotorDatabase
from redis.asyncio import Redis

from app.core.config import Settings, get_settings
from app.db.mongo import get_database
from app.db.redis import get_redis
from app.deps import get_current_character, get_current_character_optional
from app.models.character import CharacterDocument
from app.services import character_data, locations, market_prices, sde
from app.services.build_chain import material_quantity_per_run
from app.services.locations import resolve_container_chain as _resolve_container_chain
from app.templating import templates
from app.web import (
    format_isk,
    gauge_cell_html,
    icon_url,
    item_icon_url,
    location_label_html,
    location_label_text,
)

router = APIRouter(prefix="/blueprints", tags=["blueprints"])

_LIST_STYLE = ["/static/card.css", "/static/blueprints-list.css"]
_DETAIL_STYLE = ["/static/card.css", "/static/blueprints-detail.css"]


_REACTIONS_ACTIVITY_ID = 11


def _tech_level_label(is_reaction: bool, is_t2: bool) -> str:
    if is_reaction:
        return "Reaction formula"
    return "T2" if is_t2 else "T1"


_FILTER_OPTIONS = ("original", "copy", "t2")
_DEFAULT_FILTERS = frozenset({"original"})
_SORT_COLUMNS = ("name", "me", "te")
_SORT_LABELS = {
    "name": "Blueprint",
    "me": "ME",
    "te": "TE",
}


@dataclass
class _Row:
    name: str
    search_name: str
    is_copy: bool
    is_t2: bool
    me: int
    te: int
    location_id: int
    view: dict[str, object]


def _query_string(
    selected: frozenset[str], sort: str, direction: str, location: str, search: str
) -> str:
    params = [
        ("f", "1"),
        *[("show", value) for value in selected],
        ("sort", sort),
        ("dir", direction),
        *([("location", location)] if location else []),
        *([("search", search)] if search else []),
    ]
    return urlencode(params)


def _sort_link(
    column: str,
    selected: frozenset[str],
    current_sort: str,
    current_dir: str,
    location: str,
    search: str,
) -> str:
    label = _SORT_LABELS[column]
    active = column == current_sort
    if active:
        next_dir = "desc" if current_dir == "asc" else "asc"
        label += " &#9650;" if current_dir == "asc" else " &#9660;"
    else:
        next_dir = "asc"
    href = escape(f"?{_query_string(selected, column, next_dir, location, search)}")
    css_class = ' class="active"' if active else ""
    return f'<a href="{href}"{css_class}>{label}</a>'


@router.get("", response_class=HTMLResponse)
async def list_blueprints(
    request: Request,
    character: CharacterDocument = Depends(get_current_character),
    db: AsyncIOMotorDatabase = Depends(get_database),
    redis: Redis | None = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    f: str | None = Query(default=None),
    show: list[str] = Query(default=[]),
    sort: str = Query(default="name"),
    dir: str = Query(default="asc"),  # noqa: A002
    location: str = Query(default=""),
    search: str = Query(default=""),
) -> HTMLResponse:
    selected = frozenset(show) & set(_FILTER_OPTIONS) if f is not None else _DEFAULT_FILTERS
    sort = sort if sort in _SORT_COLUMNS else "name"
    direction = dir if dir in ("asc", "desc") else "asc"
    search_query = search.strip().lower()

    blueprints, corp_blueprints_included = await character_data.get_merged_blueprints(
        db, redis, settings, character
    )

    checkbox_options = [
        {
            "value": option,
            "label": "T2" if option == "t2" else option.capitalize(),
            "checked": option in selected,
        }
        for option in _FILTER_OPTIONS
    ]

    if not blueprints:
        return templates.TemplateResponse(
            request,
            "blueprints/list.html",
            {
                "character": character,
                "extra_stylesheets": _LIST_STYLE,
                "sort": sort,
                "direction": direction,
                "search": search,
                "checkbox_options": checkbox_options,
                "location_options": [],
                "corp_note": "",
                "rows": [],
                "blueprints_exist": False,
            },
        )

    sde_by_type_id = await sde.blueprint_docs(
        db, redis, settings, {bp.type_id for bp in blueprints}
    )
    product_type_ids = {
        cast(int, sde_doc["product_type_id"])
        for sde_doc in sde_by_type_id.values()
        if sde_doc.get("product_type_id") is not None
    }
    type_docs = await sde.type_docs(
        db, redis, settings, {bp.type_id for bp in blueprints} | product_type_ids
    )

    assets, corp_assets_included = await character_data.get_merged_assets(
        db, redis, settings, character
    )
    assets_by_item_id = {asset.item_id: asset for asset in assets}

    resolved_location_by_item_id = {
        bp.item_id: _resolve_container_chain(bp.location_id, assets_by_item_id) for bp in blueprints
    }
    location_info = await locations.resolve_location_info(
        db, redis, settings, character.access_token, set(resolved_location_by_item_id.values())
    )

    parsed_rows = []
    for bp in blueprints:
        is_copy = bp.quantity == -2 or bp.runs != -1
        type_doc = type_docs.get(bp.type_id, {})
        raw_name = str(type_doc.get("name", f"Type {bp.type_id}"))
        is_t2 = type_doc.get("tech_level") == 2
        status_text = "Copy" if is_copy else "Original"
        if is_copy:
            status_text += f" &middot; {bp.runs} runs"

        sde_doc = sde_by_type_id.get(bp.type_id)
        product_type_id = sde_doc.get("product_type_id") if sde_doc is not None else None

        me_gauge = gauge_cell_html(
            100.0 * bp.material_efficiency / 10, f"{bp.material_efficiency}/10"
        )
        te_gauge = gauge_cell_html(100.0 * bp.time_efficiency / 20, f"{bp.time_efficiency}/20")

        resolved_location_id = resolved_location_by_item_id[bp.item_id]
        location_label = location_label_html(
            resolved_location_id, location_info.get(resolved_location_id)
        )

        # Icon is the *output product*'s, not the blueprint's own icon.
        bg_type_id = product_type_id if product_type_id is not None else bp.type_id

        parsed_rows.append(
            _Row(
                name=raw_name,
                search_name=raw_name.lower(),
                is_copy=is_copy,
                is_t2=bool(is_t2),
                me=bp.material_efficiency,
                te=bp.time_efficiency,
                location_id=resolved_location_id,
                view={
                    "item_id": bp.item_id,
                    "bg_icon_url": item_icon_url(cast(int, bg_type_id)),
                    "name": raw_name,
                    "status_text": status_text,
                    "location_label": location_label,
                    "me_gauge": me_gauge,
                    "te_gauge": te_gauge,
                },
            )
        )

    location_options_data = sorted(
        {
            (loc_id, location_label_text(loc_id, location_info.get(loc_id)))
            for loc_id in resolved_location_by_item_id.values()
        },
        key=lambda option: option[1].lower(),
    )
    location_options = [
        {"value": str(loc_id), "label": loc_name, "selected": str(loc_id) == location}
        for loc_id, loc_name in location_options_data
    ]

    visible_rows = [
        row
        for row in parsed_rows
        if ("copy" in selected if row.is_copy else "original" in selected)
        and (not row.is_t2 or "t2" in selected)
        and (not location or str(row.location_id) == location)
        and (not search_query or search_query in row.search_name)
    ]

    sort_keys = {
        "name": lambda r: r.name.lower(),
        "me": lambda r: r.me,
        "te": lambda r: r.te,
    }
    visible_rows.sort(key=sort_keys[sort], reverse=(direction == "desc"))

    sort_links_html = "Sort by: " + " &middot; ".join(
        _sort_link(column, selected, sort, direction, location, search) for column in _SORT_COLUMNS
    )

    corp_note = (
        "Includes corporation blueprints and/or assets."
        if corp_blueprints_included or corp_assets_included
        else ""
    )

    return templates.TemplateResponse(
        request,
        "blueprints/list.html",
        {
            "character": character,
            "extra_stylesheets": _LIST_STYLE,
            "sort": sort,
            "direction": direction,
            "search": search,
            "checkbox_options": checkbox_options,
            "location_options": location_options,
            "corp_note": corp_note,
            "sort_links_html": sort_links_html,
            "rows": [row.view for row in visible_rows],
            "blueprints_exist": True,
        },
    )


@router.get("/catalog", response_class=HTMLResponse)
async def blueprint_catalog(
    request: Request,
    character: CharacterDocument | None = Depends(get_current_character_optional),
    db: AsyncIOMotorDatabase = Depends(get_database),
    q: str = Query(default=""),
) -> HTMLResponse:
    query = q.strip()

    items: list[dict[str, object]] = []
    if len(query) >= 2:
        docs = await sde.search_blueprints_by_name(db, query)
        results_state = "results" if docs else "no_match"
        items = [
            {
                "type_id": doc["_id"],
                "icon_url": item_icon_url(cast(int, doc.get("product_type_id") or doc["_id"])),
                "name": str(doc["name"]),
                "is_t2": doc.get("tech_level") == 2,
            }
            for doc in docs
        ]
    elif query:
        results_state = "too_short"
    else:
        results_state = "empty"

    return templates.TemplateResponse(
        request,
        "blueprints/catalog.html",
        {
            "character": character,
            "extra_stylesheets": _LIST_STYLE,
            "query": query,
            "results_state": results_state,
            "items": items,
        },
    )


@router.get("/catalog/{type_id}", response_class=HTMLResponse)
async def catalog_blueprint_detail(
    request: Request,
    type_id: int,
    character: CharacterDocument | None = Depends(get_current_character_optional),
    db: AsyncIOMotorDatabase = Depends(get_database),
    redis: Redis | None = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    sde_blueprints = await sde.blueprint_docs(db, redis, settings, {type_id})
    sde_blueprint = sde_blueprints.get(type_id)
    type_docs = await sde.type_docs(db, redis, settings, {type_id})
    blueprint_type_doc = type_docs.get(type_id)
    if sde_blueprint is None or blueprint_type_doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Blueprint not found")

    blueprint_name = str(blueprint_type_doc.get("name", f"Type {type_id}"))
    is_reaction = sde_blueprint.get("activity_id") == _REACTIONS_ACTIVITY_ID
    is_t2 = blueprint_type_doc.get("tech_level") == 2

    materials = cast(list[dict[str, int]], sde_blueprint["materials"])
    product_type_id = cast(int | None, sde_blueprint.get("product_type_id"))
    product_quantity = cast(int, sde_blueprint.get("product_quantity", 1))

    price_type_ids = {m["type_id"] for m in materials}
    if product_type_id is not None:
        price_type_ids.add(product_type_id)
    prices = await market_prices.list_market_prices(db, price_type_ids)
    price_by_type_id: dict[int, dict[str, object]] = {cast(int, p["_id"]): p for p in prices}

    cost_per_run = sum(
        material["quantity"] * market_prices.unit_price(price_by_type_id.get(material["type_id"]))
        for material in materials
    )
    stats = [{"value": format_isk(cost_per_run), "label": "Cost / run"}]

    product_name = ""
    if product_type_id is not None:
        product_type_docs = await sde.type_docs(db, redis, settings, {product_type_id})
        product_name = str(product_type_docs.get(product_type_id, {}).get("name", ""))
        output_per_run = product_quantity * market_prices.unit_price(
            price_by_type_id.get(product_type_id)
        )
        stats.append({"value": format_isk(output_per_run), "label": "Output / run"})
        stats.append({"value": format_isk(output_per_run - cost_per_run), "label": "Profit / run"})

    produced_text = (
        f"&middot; Produces {escape(product_name)} &times;{product_quantity}"
        if product_name
        else ""
    )

    material_type_ids = {m["type_id"] for m in materials}
    material_docs = await sde.type_docs(db, redis, settings, material_type_ids)

    def _material_name(type_id: int) -> str:
        return str(material_docs.get(type_id, {}).get("name", f"Type {type_id}"))

    return templates.TemplateResponse(
        request,
        "blueprints/catalog_detail.html",
        {
            "character": character,
            "extra_stylesheets": _DETAIL_STYLE,
            "blueprint_name": blueprint_name,
            "icon_url": icon_url(type_id),
            "tech_level_flag_class": "flag-build" if is_t2 or is_reaction else "flag-buy",
            "tech_level_label": _tech_level_label(is_reaction, is_t2),
            "produced_text": produced_text,
            "stats": stats,
            "materials": [
                {
                    "icon_url": item_icon_url(material["type_id"]),
                    "name": _material_name(material["type_id"]),
                    "quantity": material["quantity"],
                }
                for material in materials
            ],
        },
    )


@router.get("/{item_id}", response_class=HTMLResponse)
async def blueprint_detail(
    request: Request,
    item_id: int,
    character: CharacterDocument = Depends(get_current_character),
    db: AsyncIOMotorDatabase = Depends(get_database),
    redis: Redis | None = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    plan_id: str | None = Query(default=None),
) -> HTMLResponse:
    blueprints, _ = await character_data.get_merged_blueprints(db, redis, settings, character)
    blueprint = next((bp for bp in blueprints if bp.item_id == item_id), None)
    if blueprint is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Blueprint not found")

    assets, _ = await character_data.get_merged_assets(db, redis, settings, character)
    assets_by_item_id = {asset.item_id: asset for asset in assets}

    is_copy = blueprint.quantity == -2 or blueprint.runs != -1
    resolved_location_id = _resolve_container_chain(blueprint.location_id, assets_by_item_id)
    location_info = await locations.resolve_location_info(
        db, redis, settings, character.access_token, {resolved_location_id}
    )
    location_label = location_label_html(
        resolved_location_id, location_info.get(resolved_location_id)
    )

    sde_blueprints = await sde.blueprint_docs(db, redis, settings, {blueprint.type_id})
    sde_blueprint = sde_blueprints.get(blueprint.type_id)
    type_docs = await sde.type_docs(db, redis, settings, {blueprint.type_id})
    blueprint_type_name = type_docs.get(blueprint.type_id, {}).get("name")
    blueprint_name = str(blueprint_type_name or f"Type {blueprint.type_id}")

    status_text = "Copy" if is_copy else "Original"
    if is_copy:
        status_text += f" ({blueprint.runs} runs)"

    context: dict[str, Any] = {
        "character": character,
        "extra_stylesheets": _DETAIL_STYLE,
        "blueprint_name": blueprint_name,
        "blueprint_icon_url": icon_url(blueprint.type_id, is_copy),
        "material_efficiency": blueprint.material_efficiency,
        "time_efficiency": blueprint.time_efficiency,
        "status_text": status_text,
        "location_label": location_label,
        "sde_available": sde_blueprint is not None,
    }

    if sde_blueprint is None:
        return templates.TemplateResponse(request, "blueprints/detail.html", context)

    materials = cast(list[dict[str, int]], sde_blueprint["materials"])
    product_type_id = cast(int | None, sde_blueprint.get("product_type_id"))
    product_quantity = cast(int, sde_blueprint.get("product_quantity", 1))

    price_type_ids = {m["type_id"] for m in materials}
    if product_type_id is not None:
        price_type_ids.add(product_type_id)
    prices = await market_prices.list_market_prices(db, price_type_ids)
    price_by_type_id: dict[int, dict[str, object]] = {cast(int, p["_id"]): p for p in prices}

    cost_per_run = sum(
        material_quantity_per_run(m["quantity"], blueprint.material_efficiency)
        * market_prices.unit_price(price_by_type_id.get(m["type_id"]))
        for m in materials
    )
    price_figures = [{"value": format_isk(cost_per_run), "label": "Cost / run"}]
    if product_type_id is not None:
        output_per_run = product_quantity * market_prices.unit_price(
            price_by_type_id.get(product_type_id)
        )
        price_figures.append({"value": format_isk(output_per_run), "label": "Output / run"})
        price_figures.append(
            {"value": format_isk(output_per_run - cost_per_run), "label": "Profit / run"}
        )

    on_site_totals: dict[int, int] = {}
    global_totals: dict[int, int] = {}
    for asset in assets:
        global_totals[asset.type_id] = global_totals.get(asset.type_id, 0) + asset.quantity
        if asset.location_id == blueprint.location_id:
            on_site_totals[asset.type_id] = on_site_totals.get(asset.type_id, 0) + asset.quantity

    material_type_ids = {m["type_id"] for m in materials}
    material_docs = await sde.type_docs(db, redis, settings, material_type_ids)

    material_views = []
    on_site_buildable = math.inf
    global_buildable = math.inf
    for material in materials:
        type_id = material["type_id"]
        needed = material_quantity_per_run(material["quantity"], blueprint.material_efficiency)
        on_site_have = on_site_totals.get(type_id, 0)
        global_have = global_totals.get(type_id, 0)
        on_site_buildable = min(on_site_buildable, on_site_have // needed)
        global_buildable = min(global_buildable, global_have // needed)
        on_site_missing = max(0, needed - on_site_have)
        global_missing = max(0, needed - global_have)
        material_name = material_docs.get(type_id, {}).get("name")
        name = str(material_name or f"Type {type_id}")
        on_site_cell: str = str(on_site_have)
        if on_site_missing:
            on_site_cell = f'{on_site_have} <span class="short">(-{on_site_missing})</span>'
        global_cell: str = str(global_have)
        if global_missing:
            global_cell = f'{global_have} <span class="short">(-{global_missing})</span>'
        material_views.append(
            {
                "icon_url": item_icon_url(type_id),
                "name": name,
                "needed": needed,
                "on_site_cell": on_site_cell,
                "global_cell": global_cell,
            }
        )

    if not materials:
        on_site_buildable = 0
        global_buildable = 0

    add_to_plan_href = None
    if product_type_id is not None:
        if plan_id:
            add_to_plan_href = (
                f"/plans/{plan_id}/add-job?type_id={product_type_id}&qty={product_quantity}"
            )
        else:
            add_to_plan_href = f"/plans/create?type_id={product_type_id}&qty={product_quantity}"

    context.update(
        {
            "on_site_buildable": str(int(on_site_buildable)),
            "global_buildable": str(int(global_buildable)),
            "price_figures": price_figures,
            "add_to_plan_href": add_to_plan_href,
            "materials": material_views,
        }
    )
    return templates.TemplateResponse(request, "blueprints/detail.html", context)
