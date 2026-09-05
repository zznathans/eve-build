import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from app.core.config import Settings
from tests.test_blueprints_routes import _log_in

TRITANIUM_TYPE_ID = 34
WATER_TYPE_ID = 3645
SUPERCONDUCTOR_TYPE_ID = 9838
COOLANT_TYPE_ID = 9840

WATER_SCHEMATIC_ID = 70
COOLANT_SCHEMATIC_ID = 66
SUPERCONDUCTOR_SCHEMATIC_ID = 65


async def _seed_schematics(mongo_db: AsyncMongoMockClient) -> None:
    await mongo_db.sde_types.insert_many(
        [
            {"_id": TRITANIUM_TYPE_ID, "name": "Tritanium", "group_id": 18, "published": True},
            {"_id": WATER_TYPE_ID, "name": "Water", "group_id": 1042, "published": True},
            {
                "_id": SUPERCONDUCTOR_TYPE_ID,
                "name": "Superconductors",
                "group_id": 1041,
                "published": True,
            },
            {"_id": COOLANT_TYPE_ID, "name": "Coolant", "group_id": 1034, "published": True},
        ]
    )
    await mongo_db.sde_planet_schematics.insert_many(
        [
            {
                "_id": SUPERCONDUCTOR_SCHEMATIC_ID,
                "name": "Superconductors",
                "cycle_time_seconds": 3600,
                "output": {"type_id": SUPERCONDUCTOR_TYPE_ID, "quantity": 5},
                "inputs": [{"type_id": TRITANIUM_TYPE_ID, "quantity": 40}],
            },
            {
                "_id": COOLANT_SCHEMATIC_ID,
                "name": "Coolant",
                "cycle_time_seconds": 1800,
                "output": {"type_id": COOLANT_TYPE_ID, "quantity": 10},
                "inputs": [{"type_id": WATER_TYPE_ID, "quantity": 5}],
            },
            {
                "_id": WATER_SCHEMATIC_ID,
                "name": "Water",
                "cycle_time_seconds": 1800,
                "output": {"type_id": WATER_TYPE_ID, "quantity": 20},
                "inputs": [{"type_id": TRITANIUM_TYPE_ID, "quantity": 10}],
            },
        ]
    )
    await mongo_db.market_prices.insert_many(
        [
            {"_id": TRITANIUM_TYPE_ID, "average_price": 5.0},
            {"_id": WATER_TYPE_ID, "average_price": 2.0},
            {"_id": SUPERCONDUCTOR_TYPE_ID, "average_price": 1000.0},
            {"_id": COOLANT_TYPE_ID, "average_price": 50.0},
        ]
    )


@respx.mock
async def test_planetary_list_groups_by_tier_and_shows_profit(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    await _seed_schematics(mongo_db)

    response = client.get("/planetary")

    assert response.status_code == 200
    assert "Tier 2 - Refined Commodities" in response.text
    assert "Tier 4 - Advanced Commodities" in response.text
    assert "Superconductors" in response.text
    assert "Coolant" in response.text
    assert "Water" in response.text
    tier2_index = response.text.index("Tier 2 - Refined Commodities")
    tier4_index = response.text.index("Tier 4 - Advanced Commodities")
    assert tier2_index < tier4_index

    # P0 raw materials table: Tritanium is an input everywhere but never anyone's output.
    assert 'id="tier-p0"' in response.text
    assert "P0 - Raw Materials" in response.text
    assert "Tritanium" in response.text[response.text.index('id="tier-p0"') :]

    # Every section is independently toggleable.
    for section_id in ("tier-p0", "tier-1042", "tier-1034", "tier-1041"):
        assert f'id="{section_id}"' in response.text
        assert f"pi_toggle('{section_id}'" in response.text

    # Schematic rows link through to their detail page.
    assert f'href="/planetary/{COOLANT_SCHEMATIC_ID}"' in response.text

    # Profit / day: Coolant profit/cycle = 500 - 10 = 490 ISK, cycle = 1800s (48 cycles/day)
    # -> 23520 ISK/day.
    assert "Profit / day" in response.text
    assert "23.5K ISK" in response.text
    assert '<link rel="stylesheet" href="/static/card.css?v=' in response.text


@respx.mock
async def test_planetary_list_empty(
    client: TestClient,
    test_settings: Settings,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)

    response = client.get("/planetary")

    assert response.status_code == 200
    assert "No planetary schematics found" in response.text


@respx.mock
async def test_planetary_detail_tier1_has_single_from_p0_row(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    await _seed_schematics(mongo_db)

    response = client.get(f"/planetary/{WATER_SCHEMATIC_ID}")

    assert response.status_code == 200
    assert "From P0" in response.text
    assert "From P1" not in response.text
    # Water's own inputs are already raw materials: 10 Tritanium * 5.0 ISK = 50 ISK.
    # format_isk rounds sub-1000 values to whole ISK.
    assert "50 ISK" in response.text

    # Materials breakdown table for the "From P0" section shows the actual line item.
    assert "Quantity" in response.text
    assert "Unit price" in response.text
    assert "Tritanium" in response.text
    assert "10.00" in response.text  # quantity

    # Each material row is prefixed with an icon of that item.
    assert 'class="material-cell"' in response.text
    assert f"https://images.evetech.net/types/{TRITANIUM_TYPE_ID}/icon" in response.text

    # Profit / day: output value 40 - cost 50 = -10 ISK/cycle, cycle = 1800s -> -480 ISK/day.
    assert "Profit / day" in response.text
    assert "-480 ISK" in response.text
    assert '<link rel="stylesheet" href="/static/card.css?v=' in response.text


@respx.mock
async def test_planetary_detail_tier2_breaks_down_from_p0_and_p1(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    await _seed_schematics(mongo_db)

    response = client.get(f"/planetary/{COOLANT_SCHEMATIC_ID}")

    assert response.status_code == 200
    assert "From P0" in response.text
    assert "From P1" in response.text
    # From P1: 5 Water * 2.0 ISK = 10 ISK (this is just the direct input cost).
    assert "10 ISK" in response.text
    # From P0: 5 Water needs 5/20 = 0.25 Water-runs, each needing 10 Tritanium,
    # so 2.5 Tritanium * 5.0 ISK = 12.5 ISK, rounded by format_isk to 12 ISK.
    assert "12 ISK" in response.text
    # Output value is constant across rows: 10 Coolant * 50.0 ISK = 500 ISK.
    assert response.text.count("500 ISK") == 2

    # Materials breakdown: "From P1" buys 5 Water directly; "From P0" buys 2.5 Tritanium
    # (5 Water needs 5/20 = 0.25 Water-runs, each needing 10 Tritanium).
    assert "5.00" in response.text  # Water quantity, From P1 section
    assert "2.50" in response.text  # Tritanium quantity, From P0 section

    # Profit / day (cycle = 1800s, 48 cycles/day): From P1 = 490*48 = 23520 -> 23.5K ISK;
    # From P0 = 487.5*48 = 23400 -> 23.4K ISK (distinct from the From-P1 figure).
    assert "Profit / day" in response.text
    assert "23.5K ISK" in response.text
    assert "23.4K ISK" in response.text


@respx.mock
async def test_planetary_detail_404_for_unknown_schematic(
    client: TestClient,
    test_settings: Settings,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)

    response = client.get("/planetary/999999")

    assert response.status_code == 404


async def test_planetary_list_works_without_logging_in(
    client: TestClient,
    mongo_db: AsyncMongoMockClient,
) -> None:
    await _seed_schematics(mongo_db)

    response = client.get("/planetary")

    assert response.status_code == 200
    assert "Superconductors" in response.text
    assert "Log in with EVE Online" in response.text


async def test_planetary_detail_works_without_logging_in(
    client: TestClient,
    mongo_db: AsyncMongoMockClient,
) -> None:
    await _seed_schematics(mongo_db)

    response = client.get(f"/planetary/{SUPERCONDUCTOR_SCHEMATIC_ID}")

    assert response.status_code == 200
    assert "Superconductors" in response.text
    assert "Log in with EVE Online" in response.text
