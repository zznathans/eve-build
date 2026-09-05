from datetime import UTC, datetime
from html import escape
from typing import Any, cast

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
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
from app.web import (
    format_number,
    humanize_relative_time,
    item_icon_url,
    item_line_html,
    render_page,
)

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

_scope_notice_html = """<div class="page">
  <h1>Planets</h1>
  <div class="scope-notice">
    Planets needs an extra permission this character hasn't granted yet.
    <a href="/auth/logout">Log out</a> and log back in to grant access.
  </div>
</div>"""


def _planet_type_label(planet_type: str) -> str:
    return _PLANET_TYPE_LABELS.get(planet_type, planet_type.capitalize())


def _parse_esi_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


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
    character: CharacterDocument = Depends(get_current_character),
    db: AsyncIOMotorDatabase = Depends(get_database),
    redis: Redis | None = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    colonies = await _get_colonies_or_none(db, redis, settings, character)
    if colonies is None:
        return HTMLResponse(
            render_page("Planets - eve-build", _scope_notice_html, _LIST_STYLE, character=character)
        )

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

    rows_html = []
    for colony in sorted(colonies, key=lambda c: planet_names.get(c.planet_id) or ""):
        planet_name = escape(planet_names.get(colony.planet_id) or f"Planet {colony.planet_id}")
        system_name = escape(
            system_names.get(colony.solar_system_id) or f"System {colony.solar_system_id}"
        )
        type_label = escape(_planet_type_label(colony.planet_type))

        now = datetime.now(UTC)
        expiry_times = [
            _parse_esi_time(pin["expiry_time"])
            for pin in colony.pins
            if pin.get("expiry_time") is not None
        ]
        future_expiries = [t for t in expiry_times if t > now]
        if future_expiries:
            soonest = min(future_expiries)
            status_html = (
                f'<span class="flag flag-build">Extracting &middot; '
                f"ready {escape(humanize_relative_time(soonest))}</span>"
            )
        elif expiry_times:
            status_html = '<span class="flag flag-buy">Idle &middot; extraction expired</span>'
        else:
            status_html = '<span class="flag flag-buy">Idle</span>'

        extractor_count = sum(1 for pin in colony.pins if pin.get("extractor_details"))
        factory_count = sum(
            1
            for pin in colony.pins
            if pin.get("schematic_id") is not None and not pin.get("extractor_details")
        )
        # Derived from the actual pins list, not colony.num_pins - that's a separate
        # ESI summary field and shouldn't be trusted to always agree with len(pins).
        storage_count = len(colony.pins) - extractor_count - factory_count

        produces_html = ""
        produced_type_ids = produced_type_ids_by_planet[colony.planet_id]
        if produced_type_ids:
            icons_html = "".join(
                f'<img class="produces-icon" src="{escape(item_icon_url(type_id))}" '
                f'alt="{escape(_type_name(type_id))}" title="{escape(_type_name(type_id))}" '
                f"onerror=\"this.style.visibility='hidden'\">"
                for type_id in produced_type_ids
            )
            produces_html = f"""
              <div class="item-block">
                <div class="item-subhead">Produces</div>
                <div class="produces-row">{icons_html}</div>
              </div>
            """

        detail_href = escape(f"/pi/{colony.planet_id}")
        rows_html.append(f"""
          <a class="item-card" href="{detail_href}">
            <div class="item-card-content">
              <div class="item-title">{planet_name}</div>
              {produces_html}
              {item_line_html("Type", type_label)}
              {item_line_html("System", system_name)}
              {item_line_html("Upgrade level", str(colony.upgrade_level))}
              {item_line_html("Extractors", str(extractor_count))}
              {item_line_html("Factories", str(factory_count))}
              {item_line_html("Storage", str(storage_count))}
              {item_line_html("Status", status_html)}
            </div>
          </a>
        """)

    empty_slots = max(0, _MAX_COLONY_SLOTS - len(rows_html))
    rows_html.extend(['<div class="item-card item-card-empty">Empty</div>'] * empty_slots)

    body = f"""<div class="page">
      <h1>Planets</h1>
      <div class="item-grid planet-grid">{"".join(rows_html)}</div>
    </div>"""
    return HTMLResponse(render_page("Planets - eve-build", body, _LIST_STYLE, character=character))


@router.get("/{planet_id}", response_class=HTMLResponse)
async def colony_detail(
    planet_id: int,
    character: CharacterDocument = Depends(get_current_character),
    db: AsyncIOMotorDatabase = Depends(get_database),
    redis: Redis | None = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    colonies = await _get_colonies_or_none(db, redis, settings, character)
    if colonies is None:
        return HTMLResponse(
            render_page("Planets - eve-build", _scope_notice_html, _LIST_STYLE, character=character)
        )

    colony = next((c for c in colonies if c.planet_id == planet_id), None)
    if colony is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Colony not found")

    planet_names = await locations.resolve_planet_names(db, redis, settings, {colony.planet_id})
    system_names = await locations.resolve_system_names(
        db, redis, settings, {colony.solar_system_id}
    )
    planet_name = escape(planet_names.get(colony.planet_id) or f"Planet {colony.planet_id}")
    system_name = escape(
        system_names.get(colony.solar_system_id) or f"System {colony.solar_system_id}"
    )
    type_label = escape(_planet_type_label(colony.planet_type))

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

    def _card(
        title: str,
        lines: list[tuple[str, str]],
        extra_html: str = "",
        bg_type_id: int | None = None,
    ) -> str:
        lines_html = "".join(item_line_html(label, value) for label, value in lines)
        icon_html = ""
        if bg_type_id is not None:
            icon_url = escape(item_icon_url(bg_type_id))
            icon_html = (
                f'<img class="item-title-icon" src="{icon_url}" alt="" '
                f"onerror=\"this.style.visibility='hidden'\">"
            )
        return f"""
          <div class="item-card">
            <div class="item-card-content">
              <div class="item-title">{icon_html}{escape(title)}</div>
              {extra_html}
              {lines_html}
            </div>
          </div>
        """

    def _quantity_table(label: str, rows: list[tuple[str, str]]) -> str:
        rows_html = "".join(
            f"<tr><td>{name}</td><td>{quantity}</td></tr>" for name, quantity in rows
        )
        return f"""
          <div class="item-block">
            <div class="item-subhead">{escape(label)}</div>
            <table class="mini-table"><tbody>{rows_html}</tbody></table>
          </div>
        """

    now = datetime.now(UTC)
    extractor_cards = []
    factory_cards_by_type: dict[int, list[str]] = {}
    storage_cards = []
    resource_flow: dict[int, dict[str, float]] = {}

    def _add_flow(type_id: int, direction: str, quantity: float) -> None:
        entry = resource_flow.setdefault(type_id, {"in": 0.0, "out": 0.0})
        entry[direction] += quantity

    for pin in colony.pins:
        pin_type_name = _type_name(pin["type_id"])
        extractor_details = pin.get("extractor_details")
        schematic_id = pin.get("schematic_id")

        def _buffered_table(pin: dict[str, Any]) -> str:
            contents = pin.get("contents") or []
            if not contents:
                return ""
            return _quantity_table(
                "Buffered",
                [
                    (
                        escape(_type_name(item["type_id"])),
                        f"&times;{format_number(item['amount'])}",
                    )
                    for item in contents
                ],
            )

        if extractor_details is not None:
            product_type_id = extractor_details.get("product_type_id")
            product_name = escape(_type_name(product_type_id)) if product_type_id else "-"
            expiry_time = pin.get("expiry_time")
            if expiry_time is not None:
                expiry_dt = _parse_esi_time(expiry_time)
                expiry_label = (
                    escape(humanize_relative_time(expiry_dt)) if expiry_dt > now else "expired"
                )
            else:
                expiry_label = "-"
            extractor_cards.append(
                _card(
                    pin_type_name,
                    [("Product", product_name), ("Expires", expiry_label)],
                    extra_html=_buffered_table(pin),
                )
            )
            if product_type_id is not None:
                _add_flow(product_type_id, "out", extractor_details.get("qty_per_cycle", 0))
        elif schematic_id is not None and schematic_id in schematic_by_id:
            schematic = schematic_by_id[schematic_id]
            output = cast(dict[str, int], schematic["output"])
            inputs = cast(list[dict[str, int]], schematic["inputs"])
            inputs_table = _quantity_table(
                "Inputs",
                [
                    (escape(_type_name(material["type_id"])), f"&times;{material['quantity']}")
                    for material in inputs
                ],
            )
            output_table = _quantity_table(
                "Output",
                [(escape(_type_name(output["type_id"])), f"&times;{output['quantity']}")],
            )
            cycle_minutes = cast(int, schematic["cycle_time_seconds"]) // 60
            schematic_name = escape(str(schematic["name"]))
            factory_cards_by_type.setdefault(pin["type_id"], []).append(
                _card(
                    schematic_name,
                    [("Cycle time", f"{cycle_minutes} min")],
                    extra_html=inputs_table + output_table + _buffered_table(pin),
                    bg_type_id=output["type_id"],
                )
            )
            for material in inputs:
                _add_flow(material["type_id"], "in", material["quantity"])
            _add_flow(output["type_id"], "out", output["quantity"])
        else:
            contents = pin.get("contents") or []
            if contents:
                contents_table = _quantity_table(
                    "Contents",
                    [
                        (
                            escape(_type_name(item["type_id"])),
                            f"&times;{format_number(item['amount'])}",
                        )
                        for item in contents
                    ],
                )
                storage_cards.append(_card(pin_type_name, [], extra_html=contents_table))
            else:
                storage_cards.append(_card(pin_type_name, [("Contents", "-")]))

    def _section(title: str, cards_html: str) -> str:
        if not cards_html:
            return ""
        return f"""
          <div class="section-box">
            <h2>{escape(title)}</h2>
            <div class="item-grid">{cards_html}</div>
          </div>
        """

    def _grouped_section(title: str, cards_by_group: dict[int, list[str]]) -> str:
        if not cards_by_group:
            return ""
        group_type_ids = sorted(
            cards_by_group,
            key=lambda type_id: (_FACILITY_TIER_ORDER.get(type_id, 99), _type_name(type_id)),
        )
        groups_html = "".join(f"""
              <div class="facility-group">
                <h3>{escape(_type_name(group_type_id))}</h3>
                <div class="item-grid">{"".join(cards_by_group[group_type_id])}</div>
              </div>
            """ for group_type_id in group_type_ids)
        return f"""
          <div class="section-box">
            <h2>{escape(title)}</h2>
            {groups_html}
          </div>
        """

    expiry_times = [
        _parse_esi_time(pin["expiry_time"]) for pin in colony.pins if pin.get("expiry_time")
    ]
    future_expiries = [t for t in expiry_times if t > now]
    if future_expiries:
        status_html = (
            f'<span class="flag flag-build">Extracting &middot; '
            f"ready {escape(humanize_relative_time(min(future_expiries)))}</span>"
        )
    elif expiry_times:
        status_html = '<span class="flag flag-buy">Extraction expired</span>'
    else:
        status_html = '<span class="flag flag-buy">Idle</span>'

    header = f"""
      <div class="header">
        <div>
          <div class="name">{planet_name}</div>
          <div class="meta">{type_label} &middot; {system_name} &middot;
            Upgrade level {colony.upgrade_level} &middot; {colony.num_pins} pins</div>
        </div>
      </div>
    """

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

    def _flow_stat(type_id: int, direction: str) -> str:
        quantity = format_number(resource_flow[type_id][direction])
        icon = escape(item_icon_url(type_id))
        name = escape(_type_name(type_id))
        caret_class = "flow-caret-in" if direction == "in" else "flow-caret-out"
        caret_glyph = "&#9660;" if direction == "in" else "&#9650;"
        return f"""
          <div class="summary-stat summary-stat-icon">
            <img class="icon" src="{icon}" alt="{name}" onerror="this.style.visibility='hidden'">
            <span class="flow-caret {caret_class}">{caret_glyph}</span>
            <div>
              <div class="value">{quantity}</div>
              <div class="label">{name} ({direction})</div>
            </div>
          </div>
        """

    flow_stats_html = "".join(_flow_stat(type_id, "in") for type_id in import_type_ids) + "".join(
        _flow_stat(type_id, "out") for type_id in export_type_ids
    )

    summary_html = f"""
      <div class="summary">
        <div class="summary-stat">
          <div class="value">{len(extractor_cards)}</div>
          <div class="label">Extractors</div>
        </div>
        <div class="summary-stat">
          <div class="value">{sum(len(cards) for cards in factory_cards_by_type.values())}</div>
          <div class="label">Factories</div>
        </div>
        <div class="summary-stat">
          <div class="value">{len(storage_cards)}</div>
          <div class="label">Storage</div>
        </div>
        <div class="summary-stat">
          <div class="value">{status_html}</div>
          <div class="label">Status</div>
        </div>
      </div>
      {f'<div class="summary">{flow_stats_html}</div>' if flow_stats_html else ""}
    """

    body = f"""<div class="page">{header}
      {summary_html}
      <div class="section-grid">
        {_section("Extractors", "".join(extractor_cards))}
        {_grouped_section("Factories", factory_cards_by_type)}
        {_section("Storage", "".join(storage_cards))}
      </div>
      <a class="btn btn-secondary back" href="/pi">Back to Planets</a>
    </div>"""
    page_title = f"{planet_names.get(colony.planet_id) or planet_name} - eve-build"
    return HTMLResponse(render_page(page_title, body, _DETAIL_STYLE, character=character))
