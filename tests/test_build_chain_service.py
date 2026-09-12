import pytest
from mongomock_motor import AsyncMongoMockClient

from app.core.config import Settings
from app.services.build_chain import (
    aggregate_build_steps,
    aggregate_raw_materials,
    material_quantity_per_run,
    resolve_build_chain,
    structure_material_bonus,
)

TRITANIUM_TYPE_ID = 34
PYERITE_TYPE_ID = 35
COMPONENT_TYPE_ID = 500
COMPONENT_BLUEPRINT_TYPE_ID = 501
SHIP_TYPE_ID = 600
SHIP_BLUEPRINT_TYPE_ID = 601
MODULE_TYPE_ID = 700
MODULE_BLUEPRINT_TYPE_ID = 701
P0_TYPE_ID = 2073
P1_TYPE_ID = 2288
P2_TYPE_ID = 2320
P1_SCHEMATIC_ID = 100
P2_SCHEMATIC_ID = 101


async def _seed_names(mongo_db: AsyncMongoMockClient, docs: list[dict[str, object]]) -> None:
    await mongo_db.sde_types.insert_many(docs)


async def test_resolve_build_chain_single_level_has_no_sub_steps(
    mongo_db: AsyncMongoMockClient, test_settings: Settings
) -> None:
    await _seed_names(
        mongo_db,
        [
            {"_id": SHIP_TYPE_ID, "name": "Test Ship", "published": True},
            {"_id": TRITANIUM_TYPE_ID, "name": "Tritanium", "published": True},
        ],
    )
    await mongo_db.sde_blueprints.insert_one(
        {
            "_id": SHIP_BLUEPRINT_TYPE_ID,
            "product_type_id": SHIP_TYPE_ID,
            "product_quantity": 1,
            "materials": [{"type_id": TRITANIUM_TYPE_ID, "quantity": 100}],
            "activity_id": 1,
        }
    )

    resolution = await resolve_build_chain(mongo_db, None, test_settings, SHIP_TYPE_ID, 1)

    assert resolution.is_buildable is True
    assert len(resolution.steps) == 1
    assert resolution.steps[0].type_id == SHIP_TYPE_ID
    assert resolution.steps[0].runs == 1
    assert len(resolution.raw_materials) == 1
    assert resolution.raw_materials[0].type_id == TRITANIUM_TYPE_ID
    assert resolution.raw_materials[0].quantity == 100


async def test_resolve_build_chain_expands_buildable_sub_components(
    mongo_db: AsyncMongoMockClient, test_settings: Settings
) -> None:
    await _seed_names(
        mongo_db,
        [
            {"_id": SHIP_TYPE_ID, "name": "Test Ship", "published": True},
            {"_id": COMPONENT_TYPE_ID, "name": "Test Component", "published": True},
            {"_id": TRITANIUM_TYPE_ID, "name": "Tritanium", "published": True},
            {"_id": PYERITE_TYPE_ID, "name": "Pyerite", "published": True},
        ],
    )
    await mongo_db.sde_blueprints.insert_many(
        [
            {
                "_id": SHIP_BLUEPRINT_TYPE_ID,
                "product_type_id": SHIP_TYPE_ID,
                "product_quantity": 1,
                "materials": [{"type_id": COMPONENT_TYPE_ID, "quantity": 2}],
                "activity_id": 1,
            },
            {
                "_id": COMPONENT_BLUEPRINT_TYPE_ID,
                "product_type_id": COMPONENT_TYPE_ID,
                "product_quantity": 1,
                "materials": [
                    {"type_id": TRITANIUM_TYPE_ID, "quantity": 10},
                    {"type_id": PYERITE_TYPE_ID, "quantity": 5},
                ],
                "activity_id": 1,
            },
        ]
    )

    resolution = await resolve_build_chain(
        mongo_db, None, test_settings, SHIP_TYPE_ID, 1, frozenset({COMPONENT_TYPE_ID})
    )

    assert resolution.is_buildable is True
    # Ship + Component = 2 build steps; the component is listed before the ship that needs it.
    assert [step.type_id for step in resolution.steps] == [COMPONENT_TYPE_ID, SHIP_TYPE_ID]

    component_step = resolution.steps[0]
    # Ship needs 2 components, 1 component/run -> 2 runs of the component blueprint.
    assert component_step.runs == 2

    raw_by_type_id = {material.type_id: material.quantity for material in resolution.raw_materials}
    # 2 component runs * 10 Tritanium/run = 20; * 5 Pyerite/run = 10.
    assert raw_by_type_id[TRITANIUM_TYPE_ID] == 20
    assert raw_by_type_id[PYERITE_TYPE_ID] == 10


async def test_resolve_build_chain_defaults_to_collapsed_first_level(
    mongo_db: AsyncMongoMockClient, test_settings: Settings
) -> None:
    await _seed_names(
        mongo_db,
        [
            {"_id": SHIP_TYPE_ID, "name": "Test Ship", "published": True},
            {"_id": COMPONENT_TYPE_ID, "name": "Test Component", "published": True},
            {"_id": TRITANIUM_TYPE_ID, "name": "Tritanium", "published": True},
        ],
    )
    await mongo_db.sde_blueprints.insert_many(
        [
            {
                "_id": SHIP_BLUEPRINT_TYPE_ID,
                "product_type_id": SHIP_TYPE_ID,
                "product_quantity": 1,
                "materials": [{"type_id": COMPONENT_TYPE_ID, "quantity": 2}],
                "activity_id": 1,
            },
            {
                "_id": COMPONENT_BLUEPRINT_TYPE_ID,
                "product_type_id": COMPONENT_TYPE_ID,
                "product_quantity": 1,
                "materials": [{"type_id": TRITANIUM_TYPE_ID, "quantity": 10}],
                "activity_id": 1,
            },
        ]
    )

    # No build_set passed -> only the ship itself is expanded; the buildable component is
    # left as a leaf the caller can still choose to toggle to "build".
    resolution = await resolve_build_chain(mongo_db, None, test_settings, SHIP_TYPE_ID, 1)

    assert [step.type_id for step in resolution.steps] == [SHIP_TYPE_ID]
    assert len(resolution.raw_materials) == 1
    component_material = resolution.raw_materials[0]
    assert component_material.type_id == COMPONENT_TYPE_ID
    assert component_material.quantity == 2
    assert component_material.is_buildable is True


async def test_resolve_build_chain_marks_non_buildable_leaf(
    mongo_db: AsyncMongoMockClient, test_settings: Settings
) -> None:
    await _seed_names(
        mongo_db,
        [
            {"_id": SHIP_TYPE_ID, "name": "Test Ship", "published": True},
            {"_id": TRITANIUM_TYPE_ID, "name": "Tritanium", "published": True},
        ],
    )
    await mongo_db.sde_blueprints.insert_one(
        {
            "_id": SHIP_BLUEPRINT_TYPE_ID,
            "product_type_id": SHIP_TYPE_ID,
            "product_quantity": 1,
            "materials": [{"type_id": TRITANIUM_TYPE_ID, "quantity": 100}],
            "activity_id": 1,
        }
    )

    resolution = await resolve_build_chain(mongo_db, None, test_settings, SHIP_TYPE_ID, 1)

    assert resolution.raw_materials[0].is_buildable is False


async def test_resolve_build_chain_merges_shared_component_across_branches(
    mongo_db: AsyncMongoMockClient, test_settings: Settings
) -> None:
    await _seed_names(
        mongo_db,
        [
            {"_id": SHIP_TYPE_ID, "name": "Test Ship", "published": True},
            {"_id": MODULE_TYPE_ID, "name": "Test Module", "published": True},
            {"_id": COMPONENT_TYPE_ID, "name": "Shared Component", "published": True},
            {"_id": TRITANIUM_TYPE_ID, "name": "Tritanium", "published": True},
        ],
    )
    await mongo_db.sde_blueprints.insert_many(
        [
            {
                "_id": SHIP_BLUEPRINT_TYPE_ID,
                "product_type_id": SHIP_TYPE_ID,
                "product_quantity": 1,
                # Ship needs both the module and the shared component directly.
                "materials": [
                    {"type_id": MODULE_TYPE_ID, "quantity": 1},
                    {"type_id": COMPONENT_TYPE_ID, "quantity": 1},
                ],
                "activity_id": 1,
            },
            {
                "_id": MODULE_BLUEPRINT_TYPE_ID,
                "product_type_id": MODULE_TYPE_ID,
                "product_quantity": 1,
                # The module *also* needs the shared component.
                "materials": [{"type_id": COMPONENT_TYPE_ID, "quantity": 1}],
                "activity_id": 1,
            },
            {
                "_id": COMPONENT_BLUEPRINT_TYPE_ID,
                "product_type_id": COMPONENT_TYPE_ID,
                "product_quantity": 1,
                "materials": [{"type_id": TRITANIUM_TYPE_ID, "quantity": 10}],
                "activity_id": 1,
            },
        ]
    )

    resolution = await resolve_build_chain(
        mongo_db,
        None,
        test_settings,
        SHIP_TYPE_ID,
        1,
        frozenset({MODULE_TYPE_ID, COMPONENT_TYPE_ID}),
    )

    # Shared component appears as exactly one step, not two.
    component_steps = [step for step in resolution.steps if step.type_id == COMPONENT_TYPE_ID]
    assert len(component_steps) == 1
    # Demanded once directly by the ship and once via the module -> 2 runs total.
    assert component_steps[0].runs == 2

    raw_by_type_id = {material.type_id: material.quantity for material in resolution.raw_materials}
    assert raw_by_type_id[TRITANIUM_TYPE_ID] == 20


async def test_resolve_build_chain_scales_with_target_quantity(
    mongo_db: AsyncMongoMockClient, test_settings: Settings
) -> None:
    await _seed_names(
        mongo_db,
        [
            {"_id": SHIP_TYPE_ID, "name": "Test Ship", "published": True},
            {"_id": TRITANIUM_TYPE_ID, "name": "Tritanium", "published": True},
        ],
    )
    await mongo_db.sde_blueprints.insert_one(
        {
            "_id": SHIP_BLUEPRINT_TYPE_ID,
            "product_type_id": SHIP_TYPE_ID,
            "product_quantity": 1,
            "materials": [{"type_id": TRITANIUM_TYPE_ID, "quantity": 100}],
            "activity_id": 1,
        }
    )

    resolution = await resolve_build_chain(mongo_db, None, test_settings, SHIP_TYPE_ID, 3)

    assert resolution.steps[0].runs == 3
    assert resolution.raw_materials[0].quantity == 300


async def test_resolve_build_chain_marks_unbuildable_item(
    mongo_db: AsyncMongoMockClient, test_settings: Settings
) -> None:
    await _seed_names(
        mongo_db, [{"_id": TRITANIUM_TYPE_ID, "name": "Tritanium", "published": True}]
    )

    resolution = await resolve_build_chain(mongo_db, None, test_settings, TRITANIUM_TYPE_ID, 1)

    assert resolution.is_buildable is False
    assert resolution.steps == []
    # The target itself has nowhere to go but "raw" - it's the one thing you'd have to buy.
    assert [m.type_id for m in resolution.raw_materials] == [TRITANIUM_TYPE_ID]


async def test_resolve_build_chain_computes_costs_from_market_prices(
    mongo_db: AsyncMongoMockClient, test_settings: Settings
) -> None:
    await _seed_names(
        mongo_db,
        [
            {"_id": SHIP_TYPE_ID, "name": "Test Ship", "published": True},
            {"_id": TRITANIUM_TYPE_ID, "name": "Tritanium", "published": True},
        ],
    )
    await mongo_db.sde_blueprints.insert_one(
        {
            "_id": SHIP_BLUEPRINT_TYPE_ID,
            "product_type_id": SHIP_TYPE_ID,
            "product_quantity": 1,
            "materials": [{"type_id": TRITANIUM_TYPE_ID, "quantity": 100}],
            "activity_id": 1,
        }
    )
    await mongo_db.market_prices.insert_many(
        [
            {"_id": TRITANIUM_TYPE_ID, "adjusted_price": 5.0, "average_price": 5.0},
            {"_id": SHIP_TYPE_ID, "adjusted_price": 1000.0, "average_price": 1000.0},
        ]
    )

    resolution = await resolve_build_chain(mongo_db, None, test_settings, SHIP_TYPE_ID, 1)

    assert resolution.raw_material_cost == 500.0
    assert resolution.output_value == 1000.0


async def _seed_planetary_chain(mongo_db: AsyncMongoMockClient) -> None:
    await _seed_names(
        mongo_db,
        [
            {"_id": P0_TYPE_ID, "name": "Base Metals", "published": True},
            {"_id": P1_TYPE_ID, "name": "Reactive Metals", "published": True},
            {"_id": P2_TYPE_ID, "name": "Sheet Metal", "published": True},
        ],
    )
    await mongo_db.sde_planet_schematics.insert_many(
        [
            {
                "_id": P1_SCHEMATIC_ID,
                "name": "Reactive Metals",
                "cycle_time_seconds": 1800,
                "output": {"type_id": P1_TYPE_ID, "quantity": 20},
                "inputs": [{"type_id": P0_TYPE_ID, "quantity": 3000}],
            },
            {
                "_id": P2_SCHEMATIC_ID,
                "name": "Sheet Metal",
                "cycle_time_seconds": 3600,
                "output": {"type_id": P2_TYPE_ID, "quantity": 5},
                "inputs": [{"type_id": P1_TYPE_ID, "quantity": 40}],
            },
        ]
    )


async def test_resolve_build_chain_treats_planetary_material_as_buildable(
    mongo_db: AsyncMongoMockClient, test_settings: Settings
) -> None:
    await _seed_planetary_chain(mongo_db)

    # P2 is the target - it's "built" (broken down) by definition of viewing its page.
    resolution = await resolve_build_chain(mongo_db, None, test_settings, P2_TYPE_ID, 5)

    assert resolution.is_buildable is True
    assert [step.type_id for step in resolution.steps] == [P2_TYPE_ID]
    assert len(resolution.raw_materials) == 1
    p1_material = resolution.raw_materials[0]
    assert p1_material.type_id == P1_TYPE_ID
    assert p1_material.quantity == 40
    assert p1_material.is_buildable is True  # has its own schematic, toggle should be offered


async def test_resolve_build_chain_expands_planetary_material_down_to_p0(
    mongo_db: AsyncMongoMockClient, test_settings: Settings
) -> None:
    await _seed_planetary_chain(mongo_db)

    resolution = await resolve_build_chain(
        mongo_db, None, test_settings, P2_TYPE_ID, 5, frozenset({P1_TYPE_ID})
    )

    assert [step.type_id for step in resolution.steps] == [P1_TYPE_ID, P2_TYPE_ID]
    # P0 has neither a blueprint nor a schematic - it's a terminal raw material.
    assert len(resolution.raw_materials) == 1
    p0_material = resolution.raw_materials[0]
    assert p0_material.type_id == P0_TYPE_ID
    assert p0_material.is_buildable is False
    # 1 P2 run needs 40 P1 -> 2 P1 runs (20/run) -> 2 * 3000 = 6000 P0.
    assert p0_material.quantity == 6000


async def test_aggregate_raw_materials_sums_a_shared_material_across_resolutions(
    mongo_db: AsyncMongoMockClient, test_settings: Settings
) -> None:
    await _seed_names(
        mongo_db,
        [
            {"_id": SHIP_TYPE_ID, "name": "Test Ship", "published": True},
            {"_id": MODULE_TYPE_ID, "name": "Test Module", "published": True},
            {"_id": TRITANIUM_TYPE_ID, "name": "Tritanium", "published": True},
            {"_id": PYERITE_TYPE_ID, "name": "Pyerite", "published": True},
        ],
    )
    await mongo_db.sde_blueprints.insert_many(
        [
            {
                "_id": SHIP_BLUEPRINT_TYPE_ID,
                "product_type_id": SHIP_TYPE_ID,
                "product_quantity": 1,
                "materials": [{"type_id": TRITANIUM_TYPE_ID, "quantity": 100}],
                "activity_id": 1,
            },
            {
                "_id": MODULE_BLUEPRINT_TYPE_ID,
                "product_type_id": MODULE_TYPE_ID,
                "product_quantity": 1,
                "materials": [
                    {"type_id": TRITANIUM_TYPE_ID, "quantity": 50},
                    {"type_id": PYERITE_TYPE_ID, "quantity": 20},
                ],
                "activity_id": 1,
            },
        ]
    )

    ship_resolution = await resolve_build_chain(mongo_db, None, test_settings, SHIP_TYPE_ID, 1)
    module_resolution = await resolve_build_chain(mongo_db, None, test_settings, MODULE_TYPE_ID, 1)

    combined = aggregate_raw_materials([ship_resolution, module_resolution])

    by_type_id = {material.type_id: material.quantity for material in combined}
    assert by_type_id[TRITANIUM_TYPE_ID] == 150  # 100 (ship) + 50 (module)
    assert by_type_id[PYERITE_TYPE_ID] == 20  # only needed by the module
    assert len(combined) == 2


def test_material_quantity_per_run_applies_efficiency_and_rounds_up() -> None:
    assert material_quantity_per_run(100, 10) == 90
    assert material_quantity_per_run(100, 0) == 100
    # Rounds up, and never below 1 even at very high efficiency on a tiny base quantity.
    assert material_quantity_per_run(3, 10) == 3  # ceil(2.7) == 3
    assert material_quantity_per_run(1, 10) == 1


async def test_resolve_build_chain_applies_material_efficiency_to_the_targets_own_recipe(
    mongo_db: AsyncMongoMockClient, test_settings: Settings
) -> None:
    await _seed_names(
        mongo_db,
        [
            {"_id": SHIP_TYPE_ID, "name": "Test Ship", "published": True},
            {"_id": COMPONENT_TYPE_ID, "name": "Test Component", "published": True},
            {"_id": TRITANIUM_TYPE_ID, "name": "Tritanium", "published": True},
        ],
    )
    await mongo_db.sde_blueprints.insert_many(
        [
            {
                "_id": SHIP_BLUEPRINT_TYPE_ID,
                "product_type_id": SHIP_TYPE_ID,
                "product_quantity": 1,
                "materials": [{"type_id": COMPONENT_TYPE_ID, "quantity": 100}],
                "activity_id": 1,
            },
            {
                "_id": COMPONENT_BLUEPRINT_TYPE_ID,
                "product_type_id": COMPONENT_TYPE_ID,
                "product_quantity": 1,
                "materials": [{"type_id": TRITANIUM_TYPE_ID, "quantity": 100}],
                "activity_id": 1,
            },
        ]
    )

    resolution = await resolve_build_chain(
        mongo_db,
        None,
        test_settings,
        SHIP_TYPE_ID,
        1,
        frozenset({COMPONENT_TYPE_ID}),
        material_efficiency=10,
    )

    # The ship's own recipe (100 Component/run) is reduced by 10% ME -> 90.
    component_step = next(s for s in resolution.steps if s.type_id == SHIP_TYPE_ID)
    assert component_step.materials[COMPONENT_TYPE_ID] == 90
    # The component's own (unrelated, un-owned) blueprint keeps its base 0% ME quantity.
    raw_by_type_id = {m.type_id: m.quantity for m in resolution.raw_materials}
    assert raw_by_type_id[TRITANIUM_TYPE_ID] == 9000  # 90 component runs * 100 Tritanium/run


async def test_resolve_build_chain_computes_build_time_with_time_efficiency(
    mongo_db: AsyncMongoMockClient, test_settings: Settings
) -> None:
    await _seed_names(
        mongo_db,
        [
            {"_id": SHIP_TYPE_ID, "name": "Test Ship", "published": True},
            {"_id": TRITANIUM_TYPE_ID, "name": "Tritanium", "published": True},
        ],
    )
    await mongo_db.sde_blueprints.insert_one(
        {
            "_id": SHIP_BLUEPRINT_TYPE_ID,
            "product_type_id": SHIP_TYPE_ID,
            "product_quantity": 1,
            "materials": [{"type_id": TRITANIUM_TYPE_ID, "quantity": 100}],
            "manufacturing_time_seconds": 1000,
            "activity_id": 1,
        }
    )

    resolution = await resolve_build_chain(
        mongo_db, None, test_settings, SHIP_TYPE_ID, 3, time_efficiency=20
    )

    # 3 runs * 1000s/run * (1 - 20%) = 2400s.
    assert resolution.build_time_seconds == 2400.0


async def test_resolve_build_chain_build_time_is_none_when_not_buildable(
    mongo_db: AsyncMongoMockClient, test_settings: Settings
) -> None:
    await _seed_names(
        mongo_db, [{"_id": TRITANIUM_TYPE_ID, "name": "Tritanium", "published": True}]
    )

    resolution = await resolve_build_chain(mongo_db, None, test_settings, TRITANIUM_TYPE_ID, 1)

    assert resolution.build_time_seconds is None


async def test_aggregate_build_steps_sums_runs_for_a_shared_component_across_resolutions(
    mongo_db: AsyncMongoMockClient, test_settings: Settings
) -> None:
    await _seed_names(
        mongo_db,
        [
            {"_id": SHIP_TYPE_ID, "name": "Test Ship", "published": True},
            {"_id": MODULE_TYPE_ID, "name": "Test Module", "published": True},
            {"_id": COMPONENT_TYPE_ID, "name": "Shared Component", "published": True},
            {"_id": TRITANIUM_TYPE_ID, "name": "Tritanium", "published": True},
        ],
    )
    await mongo_db.sde_blueprints.insert_many(
        [
            {
                "_id": SHIP_BLUEPRINT_TYPE_ID,
                "product_type_id": SHIP_TYPE_ID,
                "product_quantity": 1,
                "materials": [{"type_id": COMPONENT_TYPE_ID, "quantity": 1}],
                "activity_id": 1,
            },
            {
                "_id": MODULE_BLUEPRINT_TYPE_ID,
                "product_type_id": MODULE_TYPE_ID,
                "product_quantity": 1,
                "materials": [{"type_id": COMPONENT_TYPE_ID, "quantity": 2}],
                "activity_id": 1,
            },
            {
                "_id": COMPONENT_BLUEPRINT_TYPE_ID,
                "product_type_id": COMPONENT_TYPE_ID,
                "product_quantity": 1,
                "materials": [{"type_id": TRITANIUM_TYPE_ID, "quantity": 10}],
                "activity_id": 1,
            },
        ]
    )

    ship_resolution = await resolve_build_chain(
        mongo_db, None, test_settings, SHIP_TYPE_ID, 1, frozenset({COMPONENT_TYPE_ID})
    )
    module_resolution = await resolve_build_chain(
        mongo_db, None, test_settings, MODULE_TYPE_ID, 1, frozenset({COMPONENT_TYPE_ID})
    )

    combined = aggregate_build_steps([ship_resolution, module_resolution])

    by_type_id = {step.type_id: step for step in combined}
    assert set(by_type_id) == {SHIP_TYPE_ID, MODULE_TYPE_ID, COMPONENT_TYPE_ID}
    # Ship needs 1 component run, module needs 2 -> 3 runs combined.
    assert by_type_id[COMPONENT_TYPE_ID].runs == 3
    assert by_type_id[COMPONENT_TYPE_ID].quantity_needed == 3
    assert by_type_id[SHIP_TYPE_ID].runs == 1
    assert by_type_id[MODULE_TYPE_ID].runs == 1


def test_structure_material_bonus_is_zero_without_an_engineering_complex() -> None:
    assert structure_material_bonus(False, "t2", "null_wh") == 0.0
    assert structure_material_bonus(False, None, "high") == 0.0


def test_structure_material_bonus_facility_only() -> None:
    assert structure_material_bonus(True, None, "high") == pytest.approx(0.01)


def test_structure_material_bonus_stacks_facility_and_rig_multiplicatively() -> None:
    # Facility 1% + T1 rig 2% in highsec: 1 - (1 - 0.01) * (1 - 0.02) = 0.0298.
    assert structure_material_bonus(True, "t1", "high") == pytest.approx(0.0298)
    # Facility 1% + T2 rig 2.4% in null/wormhole (2.4% * 2.1 = 5.04% rig bonus):
    # 1 - (1 - 0.01) * (1 - 0.0504) = 0.059896.
    assert structure_material_bonus(True, "t2", "null_wh") == pytest.approx(0.059896)
    # Facility 1% + T1 rig 2% in lowsec (2% * 1.9 = 3.8% rig bonus):
    # 1 - (1 - 0.01) * (1 - 0.038) = 0.04762.
    assert structure_material_bonus(True, "t1", "low") == pytest.approx(0.04762)


async def test_resolve_build_chain_applies_structure_bonus_to_the_targets_own_recipe(
    mongo_db: AsyncMongoMockClient, test_settings: Settings
) -> None:
    await _seed_names(
        mongo_db,
        [
            {"_id": SHIP_TYPE_ID, "name": "Test Ship", "published": True},
            {"_id": TRITANIUM_TYPE_ID, "name": "Tritanium", "published": True},
        ],
    )
    await mongo_db.sde_blueprints.insert_one(
        {
            "_id": SHIP_BLUEPRINT_TYPE_ID,
            "product_type_id": SHIP_TYPE_ID,
            "product_quantity": 1,
            "materials": [{"type_id": TRITANIUM_TYPE_ID, "quantity": 100}],
            "activity_id": 1,
        }
    )

    resolution = await resolve_build_chain(
        mongo_db,
        None,
        test_settings,
        SHIP_TYPE_ID,
        1,
        structure_bonus=structure_material_bonus(True, "t2", "null_wh"),
    )

    # 100 * (1 - 0.059896) = 94.0104 -> ceil -> 95.
    assert resolution.raw_materials[0].quantity == 95
