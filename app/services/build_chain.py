import math
from dataclasses import dataclass, field
from typing import cast

from motor.motor_asyncio import AsyncIOMotorDatabase
from redis.asyncio import Redis

from app.core.config import Settings
from app.services import market_prices, sde

_MAX_DEPTH = 15


@dataclass
class _Recipe:
    output_quantity: int
    materials: list[dict[str, int]]


async def _recipe_for_product(db: AsyncIOMotorDatabase, product_type_id: int) -> _Recipe | None:
    """A material can be produced either by a manufacturing blueprint/reaction formula, or -
    for planetary commodities (P1-P4) - by a planetary schematic that breaks it down into
    lower-tier planetary materials, bottoming out at P0 raw materials (which have neither)."""
    blueprint = await sde.blueprint_for_product(db, product_type_id)
    if blueprint is not None:
        return _Recipe(
            output_quantity=cast(int, blueprint.get("product_quantity", 1)),
            materials=cast(list[dict[str, int]], blueprint["materials"]),
        )

    schematic = await sde.planet_schematic_for_product(db, product_type_id)
    if schematic is not None:
        output = cast(dict[str, int], schematic["output"])
        return _Recipe(
            output_quantity=output["quantity"],
            materials=cast(list[dict[str, int]], schematic["inputs"]),
        )

    return None


@dataclass
class BuildStep:
    type_id: int
    name: str
    quantity_needed: int
    runs: int
    product_quantity: int
    materials: dict[int, int] = field(default_factory=dict)


@dataclass
class RawMaterial:
    type_id: int
    name: str
    quantity: int
    unit_price: float
    is_buildable: bool


@dataclass
class BuildResolution:
    target_type_id: int
    target_name: str
    target_quantity: int
    is_buildable: bool
    steps: list[BuildStep]
    raw_materials: list[RawMaterial]
    raw_material_cost: float
    output_value: float


async def resolve_build_chain(
    db: AsyncIOMotorDatabase,
    redis: Redis | None,
    settings: Settings,
    target_type_id: int,
    target_quantity: int = 1,
    build_set: frozenset[int] = frozenset(),
) -> BuildResolution:
    """Walks the build chain for a target item: finds the recipe that produces it - a
    manufacturing blueprint/reaction formula, or (for planetary commodities) a planetary
    schematic breaking it down into lower-tier planetary materials down to P0 - then expands a
    material into further build steps only if it's in build_set (the target itself is always
    expanded, since building it is the whole point of the page) - aggregating demand for shared
    components across the whole tree (a component needed by two different branches gets a
    single combined step, not two). Anything not expanded is a raw/purchasable material - the
    current leaves of the chain - whether or not it has a recipe of its own, so the caller can
    tell which leaves could be toggled to "build" versus which can only be bought."""
    steps_by_type_id: dict[int, BuildStep] = {}
    raw_totals: dict[int, int] = {}
    raw_buildable: dict[int, bool] = {}

    current_level: dict[int, int] = {target_type_id: target_quantity}
    depth = 0
    while current_level and depth < _MAX_DEPTH:
        next_level: dict[int, int] = {}
        recipe_docs = {
            type_id: recipe
            for type_id in current_level
            if (recipe := await _recipe_for_product(db, type_id)) is not None
        }

        for type_id, quantity in current_level.items():
            recipe = recipe_docs.get(type_id)
            if recipe is None or (type_id != target_type_id and type_id not in build_set):
                raw_totals[type_id] = raw_totals.get(type_id, 0) + quantity
                raw_buildable[type_id] = recipe is not None
                continue

            product_quantity = recipe.output_quantity
            materials = recipe.materials
            additional_runs = max(1, math.ceil(quantity / product_quantity))

            step = steps_by_type_id.get(type_id)
            if step is None:
                step = BuildStep(
                    type_id=type_id,
                    name="",
                    quantity_needed=0,
                    runs=0,
                    product_quantity=product_quantity,
                )
                steps_by_type_id[type_id] = step
            step.quantity_needed += quantity
            step.runs += additional_runs

            for material in materials:
                material_type_id = material["type_id"]
                needed = material["quantity"] * additional_runs
                step.materials[material_type_id] = step.materials.get(material_type_id, 0) + needed
                next_level[material_type_id] = next_level.get(material_type_id, 0) + needed

        current_level = next_level
        depth += 1

    # Hit the recursion guard while demand remained (shouldn't happen on real game data,
    # which is a DAG, but treat anything left over as raw rather than lose it).
    for type_id, quantity in current_level.items():
        raw_totals[type_id] = raw_totals.get(type_id, 0) + quantity
        raw_buildable.setdefault(type_id, False)

    all_type_ids = {target_type_id} | set(steps_by_type_id) | set(raw_totals)
    type_docs = await sde.type_docs(db, redis, settings, all_type_ids)
    prices = await market_prices.list_market_prices(db, all_type_ids)
    price_by_type_id: dict[int, dict[str, object]] = {cast(int, p["_id"]): p for p in prices}

    def _name(type_id: int) -> str:
        return str(type_docs.get(type_id, {}).get("name", f"Type {type_id}"))

    for step in steps_by_type_id.values():
        step.name = _name(step.type_id)

    raw_materials = [
        RawMaterial(
            type_id=type_id,
            name=_name(type_id),
            quantity=quantity,
            unit_price=market_prices.unit_price(price_by_type_id.get(type_id)),
            is_buildable=raw_buildable[type_id],
        )
        for type_id, quantity in sorted(raw_totals.items(), key=lambda item: _name(item[0]))
    ]
    raw_material_cost = sum(material.quantity * material.unit_price for material in raw_materials)

    target_step = steps_by_type_id.get(target_type_id)
    output_value = target_quantity * market_prices.unit_price(price_by_type_id.get(target_type_id))

    # Order steps deepest-first (the order you'd actually build in - components before the
    # thing that consumes them), by minimum distance from the raw materials.
    ordered_steps = _topological_order(target_type_id, steps_by_type_id)

    return BuildResolution(
        target_type_id=target_type_id,
        target_name=_name(target_type_id),
        target_quantity=target_quantity,
        is_buildable=target_step is not None,
        steps=ordered_steps,
        raw_materials=raw_materials,
        raw_material_cost=raw_material_cost,
        output_value=output_value,
    )


def _topological_order(
    target_type_id: int, steps_by_type_id: dict[int, BuildStep]
) -> list[BuildStep]:
    """Depth-first post-order traversal from the target down through its build steps, so a
    component is listed before anything that depends on it."""
    ordered: list[BuildStep] = []
    visited: set[int] = set()

    def _visit(type_id: int) -> None:
        if type_id in visited:
            return
        visited.add(type_id)
        step = steps_by_type_id.get(type_id)
        if step is None:
            return
        for material_type_id in step.materials:
            _visit(material_type_id)
        ordered.append(step)

    _visit(target_type_id)
    return ordered


def aggregate_raw_materials(resolutions: list[BuildResolution]) -> list[RawMaterial]:
    """Merges the raw materials needed across multiple resolutions (e.g. every job in a plan)
    into one combined shopping list, summing quantities for a material two jobs both need.
    name/unit_price/is_buildable only depend on type_id, so the first occurrence's values are
    reused rather than re-fetched."""
    merged: dict[int, RawMaterial] = {}
    for resolution in resolutions:
        for material in resolution.raw_materials:
            existing = merged.get(material.type_id)
            if existing is None:
                merged[material.type_id] = RawMaterial(
                    type_id=material.type_id,
                    name=material.name,
                    quantity=material.quantity,
                    unit_price=material.unit_price,
                    is_buildable=material.is_buildable,
                )
            else:
                existing.quantity += material.quantity

    return sorted(merged.values(), key=lambda material: material.name)
