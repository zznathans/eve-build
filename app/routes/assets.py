from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from motor.motor_asyncio import AsyncIOMotorDatabase
from redis.asyncio import Redis

from app.core.config import Settings, get_settings
from app.db.mongo import get_database
from app.db.redis import get_redis
from app.deps import get_current_character
from app.models.character import CharacterDocument
from app.services import character_data, esi, locations, market_prices, sde
from app.services.locations import resolve_container_chain
from app.templating import templates
from app.web import (
    format_isk,
    format_number,
    item_icon_url,
    location_label_html,
    location_label_text,
)

router = APIRouter(prefix="/assets", tags=["assets"])

_LIST_STYLE = ["/static/card.css", "/static/assets-list.css"]
_DETAIL_STYLE = ["/static/card.css", "/static/assets-detail.css"]

# SDE group_id for the eight raw refined minerals (Tritanium, Pyerite, Mexallon, ...).
_MINERAL_GROUP_ID = 18

# SDE category_id covering every asteroid ore variant (Veldspar, Scordite, Ice, ...).
_ORE_CATEGORY_ID = 25

# SDE group_id for research/invention datacores (Datacore - Amarrian Starship Engineering, ...).
_DATACORE_GROUP_ID = 333

# SDE group_id for the eight standard invention decryptors (Accelerant, Symmetry, ...).
_DECRYPTOR_GROUP_ID = 1304

# SDE category_ids for planetary interaction materials: raw P0 resources ("Planetary
# Resources") plus the processed P1-P4 commodities ("Planetary Commodities").
_PLANETARY_MATERIAL_CATEGORY_IDS = frozenset({42, 43})


def _group_matcher(group_id: int) -> Callable[[dict[str, object]], bool]:
    return lambda type_doc: type_doc.get("group_id") == group_id


def _category_matcher(category_id: int) -> Callable[[dict[str, object]], bool]:
    return lambda type_doc: type_doc.get("category_id") == category_id


def _categories_matcher(category_ids: frozenset[int]) -> Callable[[dict[str, object]], bool]:
    return lambda type_doc: type_doc.get("category_id") in category_ids


def _is_compressed_ore(type_doc: dict[str, object]) -> bool:
    if type_doc.get("category_id") != _ORE_CATEGORY_ID:
        return False
    return "compressed" in str(type_doc.get("name", "")).lower()


def _is_uncompressed_ore(type_doc: dict[str, object]) -> bool:
    if type_doc.get("category_id") != _ORE_CATEGORY_ID:
        return False
    return "compressed" not in str(type_doc.get("name", "")).lower()


_CATEGORIES: dict[str, Callable[[dict[str, object]], bool]] = {
    "Minerals": _group_matcher(_MINERAL_GROUP_ID),
    "Planetary Materials": _categories_matcher(_PLANETARY_MATERIAL_CATEGORY_IDS),
    "Compressed Ore": _is_compressed_ore,
    "Ore": _is_uncompressed_ore,
    "Datacores": _group_matcher(_DATACORE_GROUP_ID),
    "Decrypters": _group_matcher(_DECRYPTOR_GROUP_ID),
}

# Categories that only render when the character actually owns something in them.
_HIDE_IF_EMPTY = frozenset({"Compressed Ore"})


@dataclass
class _CategoryRow:
    type_id: int
    quantity: int
    volume: float
    value: float
    row: dict[str, object]


def _unit_volume(type_doc: dict[str, object]) -> float:
    return float(cast(float | int | None, type_doc.get("volume")) or 0.0)


def _unit_price(price_doc: dict[str, object] | None) -> float:
    return float(cast(float | int | None, (price_doc or {}).get("average_price")) or 0.0)


def _category_rows(
    assets: list[esi.AssetEntry],
    resolved_location_by_item_id: dict[int, int],
    type_docs: dict[int, dict[str, object]],
    price_by_type_id: dict[int, dict[str, object]],
    matches: Callable[[dict[str, object]], bool],
) -> list[_CategoryRow]:
    quantity_by_type: dict[int, int] = {}
    locations_by_type: dict[int, set[int]] = {}
    for asset in assets:
        type_doc = type_docs.get(asset.type_id)
        if type_doc is None or not matches(type_doc):
            continue
        quantity_by_type[asset.type_id] = quantity_by_type.get(asset.type_id, 0) + asset.quantity
        locations_by_type.setdefault(asset.type_id, set()).add(
            resolved_location_by_item_id[asset.item_id]
        )

    rows = []
    for type_id, quantity in quantity_by_type.items():
        type_doc = type_docs.get(type_id, {})
        name = str(type_doc.get("name", f"Type {type_id}"))
        row_volume = _unit_volume(type_doc) * quantity
        row_value = _unit_price(price_by_type_id.get(type_id)) * quantity
        location_count = len(locations_by_type[type_id])
        rows.append(
            _CategoryRow(
                type_id=type_id,
                quantity=quantity,
                volume=row_volume,
                value=row_value,
                row={
                    "type_id": type_id,
                    "icon_url": item_icon_url(type_id),
                    "name": name,
                    "quantity": format_number(quantity),
                    "volume": f"{format_number(row_volume)} m3",
                    "location_count": str(location_count),
                    "value": format_isk(row_value),
                },
            )
        )

    rows.sort(key=lambda row: row.volume, reverse=True)
    return rows


@router.get("", response_class=HTMLResponse)
async def list_assets(
    request: Request,
    character: CharacterDocument = Depends(get_current_character),
    db: AsyncIOMotorDatabase = Depends(get_database),
    redis: Redis | None = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    assets, corp_included = await character_data.get_merged_assets(db, redis, settings, character)

    if not assets:
        return templates.TemplateResponse(
            request,
            "assets/list.html",
            {"character": character, "extra_stylesheets": _LIST_STYLE, "sections": []},
        )

    # Resolving every location's *name* requires one ESI call per unresolved location, which is
    # slow on a first load with hundreds of locations - so the overview only ever counts location
    # ids (no ESI calls needed) and defers name resolution to the per-item locations page below.
    assets_by_item_id = {asset.item_id: asset for asset in assets}
    resolved_location_by_item_id = {
        asset.item_id: resolve_container_chain(asset.location_id, assets_by_item_id)
        for asset in assets
    }

    type_ids = {asset.type_id for asset in assets}
    type_docs = await sde.type_docs(db, redis, settings, type_ids)
    prices = await market_prices.list_market_prices(db, type_ids)
    price_by_type_id: dict[int, dict[str, object]] = {
        cast(int, price["_id"]): price for price in prices
    }

    total_quantity = sum(asset.quantity for asset in assets)
    total_volume = sum(
        _unit_volume(type_docs.get(asset.type_id, {})) * asset.quantity for asset in assets
    )
    total_value = sum(
        _unit_price(price_by_type_id.get(asset.type_id)) * asset.quantity for asset in assets
    )
    total_locations = len(set(resolved_location_by_item_id.values()))

    stats = {
        "total_items": format_number(total_quantity),
        "locations": str(total_locations),
        "total_volume": f"{format_number(total_volume)} m3",
        "total_value": format_isk(total_value),
    }

    rows_by_title = {
        title: _category_rows(
            assets, resolved_location_by_item_id, type_docs, price_by_type_id, matches
        )
        for title, matches in _CATEGORIES.items()
    }
    sections = [
        {"title": title, "rows": [entry.row for entry in rows]}
        for title, rows in rows_by_title.items()
        if rows or title not in _HIDE_IF_EMPTY
    ]

    return templates.TemplateResponse(
        request,
        "assets/list.html",
        {
            "character": character,
            "extra_stylesheets": _LIST_STYLE,
            "corp_included": corp_included,
            "stats": stats,
            "sections": sections,
        },
    )


@router.get("/{type_id}", response_class=HTMLResponse)
async def item_detail(
    request: Request,
    type_id: int,
    character: CharacterDocument = Depends(get_current_character),
    db: AsyncIOMotorDatabase = Depends(get_database),
    redis: Redis | None = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    assets, _ = await character_data.get_merged_assets(db, redis, settings, character)
    assets_by_item_id = {asset.item_id: asset for asset in assets}
    matching = [asset for asset in assets if asset.type_id == type_id]
    if not matching:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No assets of this type found")

    quantity_by_location: dict[int, int] = {}
    for asset in matching:
        location_id = resolve_container_chain(asset.location_id, assets_by_item_id)
        quantity_by_location[location_id] = (
            quantity_by_location.get(location_id, 0) + asset.quantity
        )

    # Only resolves names for the (typically handful of) locations this one item sits at,
    # unlike the overview which skips name resolution entirely - see list_assets above.
    location_info = await locations.resolve_location_info(
        db, redis, settings, character.access_token, set(quantity_by_location)
    )
    type_docs = await sde.type_docs(db, redis, settings, {type_id})
    type_doc = type_docs.get(type_id, {})
    name = str(type_doc.get("name", f"Type {type_id}"))
    unit_volume = _unit_volume(type_doc)

    price_doc = await market_prices.get_market_price(db, type_id)
    adjusted_price = float(cast(float | int | None, (price_doc or {}).get("adjusted_price")) or 0.0)
    average_price = _unit_price(price_doc)

    total_quantity = sum(quantity_by_location.values())
    total_volume = unit_volume * total_quantity
    total_value = average_price * total_quantity

    location_cards = [
        {
            "location_id": location_id,
            "location_label": location_label_html(location_id, location_info.get(location_id)),
            "quantity": format_number(quantity),
        }
        for location_id, quantity in sorted(
            quantity_by_location.items(), key=lambda item: item[1], reverse=True
        )
    ]

    return templates.TemplateResponse(
        request,
        "assets/detail.html",
        {
            "character": character,
            "extra_stylesheets": _DETAIL_STYLE,
            "name": name,
            "icon_url": item_icon_url(type_id),
            "unit_volume": format_number(unit_volume),
            "market": {
                "average_price": format_isk(average_price),
                "adjusted_price": format_isk(adjusted_price),
            },
            "owned": {
                "total_quantity": format_number(total_quantity),
                "total_volume": f"{format_number(total_volume)} m3",
                "total_value": format_isk(total_value),
            },
            "location_cards": location_cards,
        },
    )


@router.get("/locations/{location_id}", response_class=HTMLResponse)
async def location_detail(
    request: Request,
    location_id: int,
    character: CharacterDocument = Depends(get_current_character),
    db: AsyncIOMotorDatabase = Depends(get_database),
    redis: Redis | None = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    assets, _ = await character_data.get_merged_assets(db, redis, settings, character)
    assets_by_item_id = {asset.item_id: asset for asset in assets}
    matching = [
        asset
        for asset in assets
        if resolve_container_chain(asset.location_id, assets_by_item_id) == location_id
    ]
    if not matching:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No assets at this location")

    location_info = await locations.resolve_location_info(
        db, redis, settings, character.access_token, {location_id}
    )
    location_info_for_page = location_info.get(location_id)
    location_heading = location_label_html(location_id, location_info_for_page)
    location_title = location_label_text(location_id, location_info_for_page)

    type_ids = {asset.type_id for asset in matching}
    type_docs = await sde.type_docs(db, redis, settings, type_ids)
    prices = await market_prices.list_market_prices(db, type_ids)
    price_by_type_id: dict[int, dict[str, object]] = {
        cast(int, price["_id"]): price for price in prices
    }

    quantity_by_type: dict[int, int] = {}
    for asset in matching:
        quantity_by_type[asset.type_id] = quantity_by_type.get(asset.type_id, 0) + asset.quantity

    total_quantity = 0
    total_volume = 0.0
    total_value = 0.0
    entries: list[tuple[float, dict[str, Any]]] = []
    for type_id, quantity in quantity_by_type.items():
        type_doc = type_docs.get(type_id, {})
        name = str(type_doc.get("name", f"Type {type_id}"))
        row_volume = _unit_volume(type_doc) * quantity
        row_value = _unit_price(price_by_type_id.get(type_id)) * quantity
        total_quantity += quantity
        total_volume += row_volume
        total_value += row_value
        entries.append(
            (
                row_volume,
                {
                    "type_id": type_id,
                    "icon_url": item_icon_url(type_id),
                    "name": name,
                    "quantity": format_number(quantity),
                    "volume": f"{format_number(row_volume)} m3",
                    "value": format_isk(row_value),
                },
            )
        )

    entries.sort(key=lambda entry: entry[0], reverse=True)

    stats = {
        "total_items": format_number(total_quantity),
        "distinct_items": str(len(quantity_by_type)),
        "total_volume": f"{format_number(total_volume)} m3",
        "total_value": format_isk(total_value),
    }

    return templates.TemplateResponse(
        request,
        "assets/location.html",
        {
            "character": character,
            "extra_stylesheets": _LIST_STYLE,
            "location_heading": location_heading,
            "location_title": location_title,
            "stats": stats,
            "rows": [row for _, row in entries],
        },
    )
