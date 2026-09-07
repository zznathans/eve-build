from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from motor.motor_asyncio import AsyncIOMotorDatabase
from redis.asyncio import Redis

from app.core.config import Settings, get_settings
from app.db.mongo import get_database
from app.db.redis import get_redis
from app.deps import get_current_character_optional
from app.models.character import CharacterDocument
from app.services import market_prices, sde
from app.templating import templates
from app.web import format_isk, item_icon_url

router = APIRouter(prefix="/planetary", tags=["planetary"])

_LIST_STYLE = ["/static/card.css", "/static/planetary-list.css"]
_DETAIL_STYLE = ["/static/card.css", "/static/planetary-detail.css"]

_TIER_LABELS: dict[int, str] = {
    1042: "Tier 1 - Basic Commodities",
    1034: "Tier 2 - Refined Commodities",
    1040: "Tier 3 - Specialized Commodities",
    1041: "Tier 4 - Advanced Commodities",
}
_TIER_ORDER = (1042, 1034, 1040, 1041)
_TIER_INDEX_BY_GROUP_ID: dict[int, int] = {1042: 1, 1034: 2, 1040: 3, 1041: 4}
_OTHER_TIER = "Other"
_P0_LABEL = "P0 - Raw Materials"
_OTHER_GROUP_ID = 0


def _profit_per_day(profit: float, cycle_time_seconds: int) -> float:
    return profit * 86400 / cycle_time_seconds


def _collect_price_type_ids(schematics: list[dict[str, object]]) -> set[int]:
    type_ids: set[int] = set()
    for schematic in schematics:
        type_ids.add(cast(dict[str, int], schematic["output"])["type_id"])
        for material in cast(list[dict[str, int]], schematic["inputs"]):
            type_ids.add(material["type_id"])
    return type_ids


def _expand_to_tier(
    type_id: int,
    quantity: float,
    floor_tier: int,
    schematic_by_output_type_id: dict[int, dict[str, object]],
    tier_index_by_type_id: dict[int, int],
) -> dict[int, float]:
    schematic = schematic_by_output_type_id.get(type_id)
    if schematic is None or tier_index_by_type_id.get(type_id, 0) <= floor_tier:
        return {type_id: quantity}

    output = cast(dict[str, int], schematic["output"])
    runs_needed = quantity / output["quantity"]
    result: dict[int, float] = {}
    for material in cast(list[dict[str, int]], schematic["inputs"]):
        sub = _expand_to_tier(
            material["type_id"],
            runs_needed * material["quantity"],
            floor_tier,
            schematic_by_output_type_id,
            tier_index_by_type_id,
        )
        for sub_type_id, sub_quantity in sub.items():
            result[sub_type_id] = result.get(sub_type_id, 0.0) + sub_quantity
    return result


@router.get("", response_class=HTMLResponse)
async def list_planet_schematics(
    request: Request,
    character: CharacterDocument | None = Depends(get_current_character_optional),
    db: AsyncIOMotorDatabase = Depends(get_database),
    redis: Redis | None = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    schematics = await sde.list_all_planet_schematics(db)

    if not schematics:
        return templates.TemplateResponse(
            request,
            "planetary/list.html",
            {"character": character, "extra_stylesheets": _LIST_STYLE, "sections": []},
        )

    type_ids = _collect_price_type_ids(schematics)
    type_docs = await sde.type_docs(db, redis, settings, type_ids)
    prices = await market_prices.list_market_prices(db, type_ids)
    price_by_type_id: dict[int, dict[str, object]] = {cast(int, p["_id"]): p for p in prices}

    def _type_name(type_id: int) -> str:
        return str(type_docs.get(type_id, {}).get("name", f"Type {type_id}"))

    def _price(type_id: int) -> float:
        return market_prices.unit_price(price_by_type_id.get(type_id))

    output_type_ids = {cast(dict[str, int], s["output"])["type_id"] for s in schematics}
    input_type_ids = {
        material["type_id"]
        for s in schematics
        for material in cast(list[dict[str, int]], s["inputs"])
    }
    raw_material_type_ids = input_type_ids - output_type_ids

    rows_by_group_id: dict[int, list[tuple[str, dict[str, object]]]] = {}
    for schematic in schematics:
        schematic_id = schematic["_id"]
        name = str(schematic["name"])
        output = cast(dict[str, int], schematic["output"])
        inputs = cast(list[dict[str, int]], schematic["inputs"])
        output_type_id = output["type_id"]
        output_quantity = output["quantity"]

        output_value = output_quantity * market_prices.unit_price(
            price_by_type_id.get(output_type_id)
        )
        input_cost = sum(
            material["quantity"]
            * market_prices.unit_price(price_by_type_id.get(material["type_id"]))
            for material in inputs
        )
        profit = output_value - input_cost
        cycle_time_seconds = cast(int, schematic["cycle_time_seconds"])
        profit_per_day = _profit_per_day(profit, cycle_time_seconds)

        inputs_text = ", ".join(
            f"{_type_name(material['type_id'])} &times;{material['quantity']}"
            for material in inputs
        )

        row = {
            "schematic_id": schematic_id,
            "schematic_name": name,
            "icon_url": item_icon_url(output_type_id),
            "output_name": _type_name(output_type_id),
            "output_quantity": output_quantity,
            "inputs_text": inputs_text,
            "cycle_minutes": cycle_time_seconds // 60,
            "input_cost": format_isk(input_cost),
            "output_value": format_isk(output_value),
            "profit": format_isk(profit),
            "profit_per_day": format_isk(profit_per_day),
        }
        tier_group_id = cast(dict[str, object] | None, type_docs.get(output_type_id))
        group_id = cast(int | None, tier_group_id.get("group_id")) if tier_group_id else None
        group_id = group_id if group_id in _TIER_LABELS else _OTHER_GROUP_ID
        rows_by_group_id.setdefault(group_id, []).append((name, row))

    for tier_rows in rows_by_group_id.values():
        tier_rows.sort(key=lambda entry: entry[0].lower())

    section_group_ids = [gid for gid in _TIER_ORDER if gid in rows_by_group_id]
    if _OTHER_GROUP_ID in rows_by_group_id:
        section_group_ids.append(_OTHER_GROUP_ID)

    sections: list[dict[str, Any]] = []

    if raw_material_type_ids:
        raw_type_ids = sorted(raw_material_type_ids, key=lambda tid: _type_name(tid).lower())
        sections.append(
            {
                "section_id": "tier-p0",
                "label": _P0_LABEL,
                "kind": "p0",
                "rows": [
                    {
                        "icon_url": item_icon_url(type_id),
                        "name": _type_name(type_id),
                        "price": format_isk(_price(type_id)),
                    }
                    for type_id in raw_type_ids
                ],
            }
        )

    for group_id in section_group_ids:
        tier_name = _TIER_LABELS.get(group_id, _OTHER_TIER)
        section_id = "tier-other" if group_id == _OTHER_GROUP_ID else f"tier-{group_id}"
        sections.append(
            {
                "section_id": section_id,
                "label": tier_name,
                "kind": "tier",
                "rows": [row for _, row in rows_by_group_id[group_id]],
            }
        )

    return templates.TemplateResponse(
        request,
        "planetary/list.html",
        {"character": character, "extra_stylesheets": _LIST_STYLE, "sections": sections},
    )


@router.get("/{schematic_id}", response_class=HTMLResponse)
async def planet_schematic_detail(
    request: Request,
    schematic_id: int,
    character: CharacterDocument | None = Depends(get_current_character_optional),
    db: AsyncIOMotorDatabase = Depends(get_database),
    redis: Redis | None = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    schematics = await sde.list_all_planet_schematics(db)
    schematic = next((s for s in schematics if s["_id"] == schematic_id), None)
    if schematic is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Planetary schematic not found")

    page_title = f"{schematic['name']} - eve-build"

    type_ids = _collect_price_type_ids(schematics)
    type_docs = await sde.type_docs(db, redis, settings, type_ids)
    prices = await market_prices.list_market_prices(db, type_ids)
    price_by_type_id: dict[int, dict[str, object]] = {cast(int, p["_id"]): p for p in prices}

    def _type_name(type_id: int) -> str:
        return str(type_docs.get(type_id, {}).get("name", f"Type {type_id}"))

    schematic_by_output_type_id = {
        cast(dict[str, int], s["output"])["type_id"]: s for s in schematics
    }
    tier_index_by_type_id: dict[int, int] = {}
    for output_type_id in schematic_by_output_type_id:
        type_doc = type_docs.get(output_type_id)
        group_id = cast(int | None, type_doc.get("group_id")) if type_doc else None
        if group_id in _TIER_INDEX_BY_GROUP_ID:
            tier_index_by_type_id[output_type_id] = _TIER_INDEX_BY_GROUP_ID[group_id]

    output = cast(dict[str, int], schematic["output"])
    inputs = cast(list[dict[str, int]], schematic["inputs"])
    output_type_id = output["type_id"]
    output_quantity = output["quantity"]
    output_value = output_quantity * market_prices.unit_price(price_by_type_id.get(output_type_id))
    cycle_time_seconds = cast(int, schematic["cycle_time_seconds"])
    cycle_minutes = cycle_time_seconds // 60

    context: dict[str, Any] = {
        "character": character,
        "extra_stylesheets": _DETAIL_STYLE,
        "page_title": page_title,
        "icon_url": item_icon_url(output_type_id),
        "output_name": _type_name(output_type_id),
        "schematic_name": str(schematic["name"]),
        "output_quantity": output_quantity,
        "cycle_minutes": cycle_minutes,
    }

    own_tier = tier_index_by_type_id.get(output_type_id, 0)
    context["tier_data_available"] = own_tier >= 1
    if own_tier < 1:
        return templates.TemplateResponse(request, "planetary/detail.html", context)

    summary_rows = []
    detail_sections = []
    for floor in range(own_tier):
        expanded: dict[int, float] = {}
        for material in inputs:
            sub = _expand_to_tier(
                material["type_id"],
                material["quantity"],
                floor,
                schematic_by_output_type_id,
                tier_index_by_type_id,
            )
            for type_id, quantity in sub.items():
                expanded[type_id] = expanded.get(type_id, 0.0) + quantity

        cost = sum(
            quantity * market_prices.unit_price(price_by_type_id.get(type_id))
            for type_id, quantity in expanded.items()
        )
        profit = output_value - cost
        profit_per_day = _profit_per_day(profit, cycle_time_seconds)
        tier_label = f"From P{floor}"

        summary_rows.append(
            {
                "tier_label": tier_label,
                "cost": format_isk(cost),
                "output_value": format_isk(output_value),
                "profit": format_isk(profit),
                "profit_per_day": format_isk(profit_per_day),
            }
        )

        material_lines = sorted(
            (
                (type_id, quantity, market_prices.unit_price(price_by_type_id.get(type_id)))
                for type_id, quantity in expanded.items()
            ),
            key=lambda line: _type_name(line[0]).lower(),
        )
        detail_sections.append(
            {
                "tier_label": tier_label,
                "rows": [
                    {
                        "icon_url": item_icon_url(type_id),
                        "name": _type_name(type_id),
                        "quantity": f"{quantity:,.2f}",
                        "unit_price": format_isk(unit_price),
                        "subtotal": format_isk(quantity * unit_price),
                    }
                    for type_id, quantity, unit_price in material_lines
                ],
            }
        )

    context["summary_rows"] = summary_rows
    context["detail_sections"] = detail_sections
    return templates.TemplateResponse(request, "planetary/detail.html", context)
