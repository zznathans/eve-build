from datetime import UTC, datetime
from typing import Any, cast

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from motor.motor_asyncio import AsyncIOMotorDatabase
from redis.asyncio import Redis

from app.core.config import Settings, get_settings
from app.db.mongo import get_database
from app.db.redis import get_redis
from app.deps import get_current_character
from app.models.character import CharacterDocument
from app.services import character_data, locations, sde
from app.services.esi import ColonyRecord
from app.templating import templates
from app.web import format_number, humanize_relative_time, item_icon_url

router = APIRouter(prefix="/pi", tags=["pi"])

_PI_SCOPE = "esi-planets.manage_planets.v1"
# EVE's hard cap on planetary command centers per character - the list page always
# reserves this many grid cells, filling unused ones with an empty placeholder.
_MAX_COLONY_SLOTS = 6

_LIST_STYLE = ["/static/card.css", "/static/pi-list.css"]
_DETAIL_STYLE = ["/static/card.css", "/static/pi-detail.css"]

# Keyed by type_id rather than name - immune to any casing/locale differences in the
# resolved SDE type name, unlike matching on the display string.
_FACILITY_TIER_ORDER: dict[int, int] = {
    2470: 0,  # Basic Industry Facility
    2471: 1,  # Advanced Industry Facility
    2477: 2,  # High-Tech Production Plant
}

_PLANET_TYPE_LABELS: dict[str, str] = {
    "temperate": "Temperate",
    "barren": "Barren",
    "oceanic": "Oceanic",
    "ice": "Ice",
    "gas": "Gas",
    "lava": "Lava",
    "storm": "Storm",
    "plasma": "Plasma",
}


def _planet_type_label(planet_type: str) -> str:
    return _PLANET_TYPE_LABELS.get(planet_type, planet_type.capitalize())


def _parse_esi_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _format_synced_at(value: str) -> str:
    """CCP only recalculates a colony's pins (including storage contents) when the
    colony is viewed in the EVE client - ESI won't show newer data than this
    regardless of how often eve-build polls it, so this timestamp (not "now") is
    the honest answer to "how fresh is this"."""
    return _parse_esi_time(value).strftime("%Y-%m-%d %H:%M UTC")


async def _get_colonies_or_none(
    db: AsyncIOMotorDatabase,
    redis: Redis | None,
    settings: Settings,
    character: CharacterDocument,
) -> list[ColonyRecord] | None:
    """None means this character's session doesn't carry the PI scope (either it was
    never granted, or ESI itself rejected the call with a 401/403) - callers should
    show the re-login notice rather than an empty colony list."""
    if _PI_SCOPE not in character.scopes:
        return None
    try:
        return await character_data.get_character_colonies(
            db, redis, settings, character.access_token, character.character_id
        )
    except httpx.HTTPStatusError as error:
        if error.response.status_code in (401, 403):
            return None
        raise


@router.get("", response_class=HTMLResponse)
async def list_colonies(
    request: Request,
    character: CharacterDocument = Depends(get_current_character),
    db: AsyncIOMotorDatabase = Depends(get_database),
    redis: Redis | None = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    colonies = await _get_colonies_or_none(db, redis, settings, character)
    if colonies is None:
        return templates.TemplateResponse(request, "pi/scope_notice.html", {"character": character})

    planet_names = await locations.resolve_planet_names(
        db, redis, settings, {colony.planet_id for colony in colonies}
    )
    system_names = await locations.resolve_system_names(
        db, redis, settings, {colony.solar_system_id for colony in colonies}
    )

    schematics = await sde.list_all_planet_schematics(db)
    schematic_by_id = {schematic["_id"]: schematic for schematic in schematics}

    def _produced_type_ids(colony: ColonyRecord) -> list[int]:
        """Distinct products this colony makes, in pin order - every extractor's
        product and every factory's schematic output, deduped but not filtered for
        intermediates (unlike the detail page's net resource flow), since this is
        just a quick "what does this planet make" glance."""
        produced: list[int] = []
        seen: set[int] = set()
        for pin in colony.pins:
            extractor_details = pin.get("extractor_details")
            schematic_id = pin.get("schematic_id")
            product_type_id: int | None = None
            if extractor_details is not None:
                product_type_id = extractor_details.get("product_type_id")
            elif schematic_id is not None and schematic_id in schematic_by_id:
                output = cast(dict[str, int], schematic_by_id[schematic_id]["output"])
                product_type_id = output["type_id"]
            if product_type_id is not None and product_type_id not in seen:
                seen.add(product_type_id)
                produced.append(product_type_id)
        return produced

    produced_type_ids_by_planet = {
        colony.planet_id: _produced_type_ids(colony) for colony in colonies
    }
    all_produced_type_ids = {
        type_id for ids in produced_type_ids_by_planet.values() for type_id in ids
    }
    type_docs = await sde.type_docs(db, redis, settings, all_produced_type_ids)

    def _type_name(type_id: int) -> str:
        return str(type_docs.get(type_id, {}).get("name", f"Type {type_id}"))

    colonies_view = []
    for colony in sorted(colonies, key=lambda c: planet_names.get(c.planet_id) or ""):
        now = datetime.now(UTC)
        expiry_times = [
            _parse_esi_time(pin["expiry_time"])
            for pin in colony.pins
            if pin.get("expiry_time") is not None
        ]
        future_expiries = [t for t in expiry_times if t > now]
        if future_expiries:
            status_kind = "extracting"
            status_ready_label = humanize_relative_time(min(future_expiries))
        elif expiry_times:
            status_kind = "idle_expired"
            status_ready_label = None
        else:
            status_kind = "idle"
            status_ready_label = None

        extractor_count = sum(1 for pin in colony.pins if pin.get("extractor_details"))
        factory_count = sum(
            1
            for pin in colony.pins
            if pin.get("schematic_id") is not None and not pin.get("extractor_details")
        )
        # Derived from the actual pins list, not colony.num_pins - that's a separate
        # ESI summary field and shouldn't be trusted to always agree with len(pins).
        storage_count = len(colony.pins) - extractor_count - factory_count

        produced_items = [
            {"type_id": type_id, "name": _type_name(type_id), "icon_url": item_icon_url(type_id)}
            for type_id in produced_type_ids_by_planet[colony.planet_id]
        ]

        colonies_view.append(
            {
                "planet_id": colony.planet_id,
                "planet_name": planet_names.get(colony.planet_id) or f"Planet {colony.planet_id}",
                "system_name": system_names.get(colony.solar_system_id)
                or f"System {colony.solar_system_id}",
                "type_label": _planet_type_label(colony.planet_type),
                "upgrade_level": colony.upgrade_level,
                "extractor_count": extractor_count,
                "factory_count": factory_count,
                "storage_count": storage_count,
                "status_kind": status_kind,
                "status_ready_label": status_ready_label,
                "produced_items": produced_items,
                "last_synced": _format_synced_at(colony.last_update),
            }
        )

    empty_slots = max(0, _MAX_COLONY_SLOTS - len(colonies_view))
    return templates.TemplateResponse(
        request,
        "pi/list.html",
        {
            "character": character,
            "extra_stylesheets": _LIST_STYLE,
            "colonies": colonies_view,
            "empty_slots": empty_slots,
        },
    )


@router.get("/{planet_id}", response_class=HTMLResponse)
async def colony_detail(
    request: Request,
    planet_id: int,
    character: CharacterDocument = Depends(get_current_character),
    db: AsyncIOMotorDatabase = Depends(get_database),
    redis: Redis | None = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    colonies = await _get_colonies_or_none(db, redis, settings, character)
    if colonies is None:
        return templates.TemplateResponse(request, "pi/scope_notice.html", {"character": character})

    colony = next((c for c in colonies if c.planet_id == planet_id), None)
    if colony is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Colony not found")

    planet_names = await locations.resolve_planet_names(db, redis, settings, {colony.planet_id})
    system_names = await locations.resolve_system_names(
        db, redis, settings, {colony.solar_system_id}
    )
    planet_name = planet_names.get(colony.planet_id) or f"Planet {colony.planet_id}"
    system_name = system_names.get(colony.solar_system_id) or f"System {colony.solar_system_id}"
    type_label = _planet_type_label(colony.planet_type)

    schematics = await sde.list_all_planet_schematics(db)
    schematic_by_id = {schematic["_id"]: schematic for schematic in schematics}

    pin_type_ids = {cast(int, pin["type_id"]) for pin in colony.pins}
    schematic_product_type_ids = {
        cast(dict[str, int], s["output"])["type_id"]
        for s in schematic_by_id.values()
        if s["_id"] in {pin.get("schematic_id") for pin in colony.pins}
    }
    schematic_input_type_ids = {
        material["type_id"]
        for s in schematic_by_id.values()
        if s["_id"] in {pin.get("schematic_id") for pin in colony.pins}
        for material in cast(list[dict[str, int]], s["inputs"])
    }
    extractor_product_type_ids = {
        pin["extractor_details"]["product_type_id"]
        for pin in colony.pins
        if pin.get("extractor_details") is not None
        and pin["extractor_details"].get("product_type_id") is not None
    }
    contents_type_ids = {
        item["type_id"] for pin in colony.pins for item in (pin.get("contents") or [])
    }
    type_ids = (
        pin_type_ids
        | schematic_product_type_ids
        | schematic_input_type_ids
        | extractor_product_type_ids
        | contents_type_ids
    )
    type_docs = await sde.type_docs(db, redis, settings, type_ids)

    def _type_name(type_id: int) -> str:
        return str(type_docs.get(type_id, {}).get("name", f"Type {type_id}"))

    def _quantity_rows(items: list[dict[str, Any]]) -> list[dict[str, str]]:
        return [
            {"name": _type_name(item["type_id"]), "amount": format_number(item["amount"])}
            for item in items
        ]

    now = datetime.now(UTC)
    extractor_cards = []
    factory_cards_by_type: dict[int, list[dict[str, Any]]] = {}
    storage_cards = []
    resource_flow: dict[int, dict[str, float]] = {}

    def _add_flow(type_id: int, direction: str, quantity: float) -> None:
        entry = resource_flow.setdefault(type_id, {"in": 0.0, "out": 0.0})
        entry[direction] += quantity

    for pin in colony.pins:
        pin_type_name = _type_name(pin["type_id"])
        extractor_details = pin.get("extractor_details")
        schematic_id = pin.get("schematic_id")
        contents = pin.get("contents") or []
        buffered = _quantity_rows(contents) if contents else None

        if extractor_details is not None:
            product_type_id = extractor_details.get("product_type_id")
            product_name = _type_name(product_type_id) if product_type_id else "-"
            expiry_time = pin.get("expiry_time")
            if expiry_time is not None:
                expiry_dt = _parse_esi_time(expiry_time)
                expiry_label = humanize_relative_time(expiry_dt) if expiry_dt > now else "expired"
            else:
                expiry_label = "-"
            extractor_cards.append(
                {
                    "name": pin_type_name,
                    "product_name": product_name,
                    "expiry_label": expiry_label,
                    "buffered": buffered,
                }
            )
            if product_type_id is not None:
                _add_flow(product_type_id, "out", extractor_details.get("qty_per_cycle", 0))
        elif schematic_id is not None and schematic_id in schematic_by_id:
            schematic = schematic_by_id[schematic_id]
            output = cast(dict[str, int], schematic["output"])
            inputs = cast(list[dict[str, int]], schematic["inputs"])
            inputs_rows = [
                {"name": _type_name(material["type_id"]), "amount": str(material["quantity"])}
                for material in inputs
            ]
            output_rows = [
                {"name": _type_name(output["type_id"]), "amount": str(output["quantity"])}
            ]
            cycle_minutes = cast(int, schematic["cycle_time_seconds"]) // 60
            factory_cards_by_type.setdefault(pin["type_id"], []).append(
                {
                    "schematic_name": str(schematic["name"]),
                    "cycle_minutes": cycle_minutes,
                    "inputs": inputs_rows,
                    "output": output_rows,
                    "bg_icon_url": item_icon_url(output["type_id"]),
                    "buffered": buffered,
                }
            )
            for material in inputs:
                _add_flow(material["type_id"], "in", material["quantity"])
            _add_flow(output["type_id"], "out", output["quantity"])
        else:
            storage_cards.append(
                {
                    "name": pin_type_name,
                    "contents": _quantity_rows(contents) if contents else None,
                }
            )

    factory_group_type_ids = sorted(
        factory_cards_by_type,
        key=lambda type_id: (_FACILITY_TIER_ORDER.get(type_id, 99), _type_name(type_id)),
    )
    factory_groups = [
        {"name": _type_name(group_type_id), "cards": factory_cards_by_type[group_type_id]}
        for group_type_id in factory_group_type_ids
    ]
    factory_count = sum(len(cards) for cards in factory_cards_by_type.values())

    expiry_times = [
        _parse_esi_time(pin["expiry_time"]) for pin in colony.pins if pin.get("expiry_time")
    ]
    future_expiries = [t for t in expiry_times if t > now]
    if future_expiries:
        status_kind = "extracting"
        status_ready_label = humanize_relative_time(min(future_expiries))
    elif expiry_times:
        status_kind = "expired"
        status_ready_label = None
    else:
        status_kind = "idle"
        status_ready_label = None

    resource_names = sorted(resource_flow, key=lambda type_id: _type_name(type_id).lower())
    # A resource produced AND consumed somewhere on this colony is an intermediate -
    # excluded from both sides. Only raw inputs (consumed, never produced here) and
    # final outputs (produced, never consumed here) are shown.
    import_type_ids = [
        type_id
        for type_id in resource_names
        if resource_flow[type_id]["in"] and not resource_flow[type_id]["out"]
    ]
    export_type_ids = [
        type_id
        for type_id in resource_names
        if resource_flow[type_id]["out"] and not resource_flow[type_id]["in"]
    ]

    def _flow_stat(type_id: int, direction: str) -> dict[str, Any]:
        return {
            "type_id": type_id,
            "name": _type_name(type_id),
            "icon_url": item_icon_url(type_id),
            "quantity": format_number(resource_flow[type_id][direction]),
            "direction": direction,
        }

    flow_stats = [_flow_stat(type_id, "in") for type_id in import_type_ids] + [
        _flow_stat(type_id, "out") for type_id in export_type_ids
    ]

    page_title = f"{planet_names.get(colony.planet_id) or planet_name} - eve-build"
    return templates.TemplateResponse(
        request,
        "pi/detail.html",
        {
            "character": character,
            "extra_stylesheets": _DETAIL_STYLE,
            "page_title": page_title,
            "planet_name": planet_name,
            "type_label": type_label,
            "system_name": system_name,
            "upgrade_level": colony.upgrade_level,
            "num_pins": colony.num_pins,
            "last_synced": _format_synced_at(colony.last_update),
            "extractor_cards": extractor_cards,
            "factory_groups": factory_groups,
            "factory_count": factory_count,
            "storage_cards": storage_cards,
            "status_kind": status_kind,
            "status_ready_label": status_ready_label,
            "flow_stats": flow_stats,
        },
    )
