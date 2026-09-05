import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from httpx import Response
from mongomock_motor import AsyncMongoMockClient

from app.core.config import Settings
from tests.test_blueprints_routes import CHARACTER_ID, _log_in

PI_SCOPE = "esi-planets.manage_planets.v1"

PLANET_ID = 4001
SYSTEM_ID = 30000142
WATER_TYPE_ID = 3645
COOLANT_TYPE_ID = 9840
EXTRACTOR_TYPE_ID = 2848
FACTORY_TYPE_ID = 2470
STORAGE_TYPE_ID = 2536
COOLANT_SCHEMATIC_ID = 66

EXTRACTOR_PIN_ID = 1_000_000_000_001
FACTORY_PIN_ID = 1_000_000_000_002
STORAGE_PIN_ID = 1_000_000_000_003


def _mock_colonies(settings: Settings, *, future_expiry: bool = True) -> None:
    expiry = "2099-01-01T00:00:00Z" if future_expiry else "2000-01-01T00:00:00Z"
    respx.get(f"{settings.esi_base_url}/characters/{CHARACTER_ID}/planets/").mock(
        return_value=Response(
            200,
            json=[
                {
                    "planet_id": PLANET_ID,
                    "solar_system_id": SYSTEM_ID,
                    "planet_type": "gas",
                    "owner_id": CHARACTER_ID,
                    "last_update": "2026-01-01T00:00:00Z",
                    "upgrade_level": 3,
                    "num_pins": 3,
                }
            ],
        )
    )
    respx.get(f"{settings.esi_base_url}/characters/{CHARACTER_ID}/planets/{PLANET_ID}/").mock(
        return_value=Response(
            200,
            json={
                "links": [
                    {
                        "source_pin_id": EXTRACTOR_PIN_ID,
                        "destination_pin_id": FACTORY_PIN_ID,
                        "link_level": 0,
                    }
                ],
                "pins": [
                    {
                        "pin_id": EXTRACTOR_PIN_ID,
                        "type_id": EXTRACTOR_TYPE_ID,
                        "expiry_time": expiry,
                        "extractor_details": {
                            "product_type_id": WATER_TYPE_ID,
                            "qty_per_cycle": 100,
                        },
                    },
                    {
                        "pin_id": FACTORY_PIN_ID,
                        "type_id": FACTORY_TYPE_ID,
                        "schematic_id": COOLANT_SCHEMATIC_ID,
                    },
                    {
                        "pin_id": STORAGE_PIN_ID,
                        "type_id": STORAGE_TYPE_ID,
                        "contents": [{"type_id": WATER_TYPE_ID, "amount": 500}],
                    },
                ],
                "routes": [
                    {
                        "source_pin_id": FACTORY_PIN_ID,
                        "destination_pin_id": STORAGE_PIN_ID,
                        "content_type_id": COOLANT_TYPE_ID,
                        "quantity": 10,
                        "route_id": 1,
                        "waypoints": [],
                    }
                ],
            },
        )
    )
    respx.get(f"{settings.esi_base_url}/universe/planets/{PLANET_ID}").mock(
        return_value=Response(200, json={"name": "Jita IV", "type_id": 2016})
    )
    respx.get(f"{settings.esi_base_url}/universe/systems/{SYSTEM_ID}").mock(
        return_value=Response(200, json={"name": "Jita"})
    )


def _mock_colonies_with_backed_up_route(settings: Settings) -> None:
    """Same fixture as _mock_colonies, but the factory pin has material buffered in its
    own contents - as if the route to storage were broken or backed up."""
    respx.get(f"{settings.esi_base_url}/characters/{CHARACTER_ID}/planets/").mock(
        return_value=Response(
            200,
            json=[
                {
                    "planet_id": PLANET_ID,
                    "solar_system_id": SYSTEM_ID,
                    "planet_type": "gas",
                    "owner_id": CHARACTER_ID,
                    "last_update": "2026-01-01T00:00:00Z",
                    "upgrade_level": 3,
                    "num_pins": 2,
                }
            ],
        )
    )
    respx.get(f"{settings.esi_base_url}/characters/{CHARACTER_ID}/planets/{PLANET_ID}/").mock(
        return_value=Response(
            200,
            json={
                "links": [],
                "pins": [
                    {
                        "pin_id": EXTRACTOR_PIN_ID,
                        "type_id": EXTRACTOR_TYPE_ID,
                        "expiry_time": "2099-01-01T00:00:00Z",
                        "extractor_details": {
                            "product_type_id": WATER_TYPE_ID,
                            "qty_per_cycle": 100,
                        },
                        "contents": [{"type_id": WATER_TYPE_ID, "amount": 300}],
                    },
                    {
                        "pin_id": FACTORY_PIN_ID,
                        "type_id": FACTORY_TYPE_ID,
                        "schematic_id": COOLANT_SCHEMATIC_ID,
                        "contents": [{"type_id": COOLANT_TYPE_ID, "amount": 40}],
                    },
                ],
                "routes": [],
            },
        )
    )
    respx.get(f"{settings.esi_base_url}/universe/planets/{PLANET_ID}").mock(
        return_value=Response(200, json={"name": "Jita IV", "type_id": 2016})
    )
    respx.get(f"{settings.esi_base_url}/universe/systems/{SYSTEM_ID}").mock(
        return_value=Response(200, json={"name": "Jita"})
    )


async def _seed_types(mongo_db: AsyncMongoMockClient) -> None:
    await mongo_db.sde_types.insert_many(
        [
            {"_id": WATER_TYPE_ID, "name": "Water", "group_id": 1042, "published": True},
            {"_id": COOLANT_TYPE_ID, "name": "Coolant", "group_id": 1034, "published": True},
            {
                "_id": EXTRACTOR_TYPE_ID,
                "name": "Extractor Control Unit",
                "group_id": 1029,
                "published": True,
            },
            {
                "_id": FACTORY_TYPE_ID,
                "name": "Basic Industry Facility",
                "group_id": 1030,
                "published": True,
            },
            {
                "_id": STORAGE_TYPE_ID,
                "name": "Storage Facility",
                "group_id": 1031,
                "published": True,
            },
        ]
    )
    await mongo_db.sde_planet_schematics.insert_many(
        [
            {
                "_id": COOLANT_SCHEMATIC_ID,
                "name": "Coolant",
                "cycle_time_seconds": 1800,
                "output": {"type_id": COOLANT_TYPE_ID, "quantity": 10},
                "inputs": [{"type_id": WATER_TYPE_ID, "quantity": 5}],
            }
        ]
    )


@respx.mock
async def test_pi_list_shows_colony_and_extraction_status(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair, scopes=[PI_SCOPE])
    await _seed_types(mongo_db)
    _mock_colonies(test_settings)

    response = client.get("/pi")

    assert response.status_code == 200
    assert "Jita IV" in response.text
    assert "Gas" in response.text
    assert "Jita" in response.text
    assert "Extracting" in response.text
    assert f'href="/pi/{PLANET_ID}"' in response.text


@respx.mock
async def test_pi_list_shows_idle_when_extraction_expired(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair, scopes=[PI_SCOPE])
    await _seed_types(mongo_db)
    _mock_colonies(test_settings, future_expiry=False)

    response = client.get("/pi")

    assert response.status_code == 200
    assert "extraction expired" in response.text


@respx.mock
async def test_pi_detail_shows_extractor_factory_storage(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair, scopes=[PI_SCOPE])
    await _seed_types(mongo_db)
    _mock_colonies(test_settings)

    response = client.get(f"/pi/{PLANET_ID}")

    assert response.status_code == 200
    assert "Jita IV" in response.text
    assert "Extractors" in response.text
    assert "Water" in response.text
    assert "Factories" in response.text
    assert "Coolant" in response.text
    assert "Storage" in response.text
    assert "Links" not in response.text
    assert "Routes" not in response.text


@respx.mock
async def test_pi_detail_shows_buffered_contents_on_extractor_and_factory(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    """Material can sit in an extractor's or factory's own buffer (e.g. a broken/backed-up
    route to storage) - it must still show up somewhere, not be silently dropped."""
    _log_in(client, test_settings, rsa_key_pair, scopes=[PI_SCOPE])
    await _seed_types(mongo_db)
    _mock_colonies_with_backed_up_route(test_settings)

    response = client.get(f"/pi/{PLANET_ID}")

    assert response.status_code == 200
    assert "Buffered" in response.text
    assert response.text.count("Water") >= 2
    assert response.text.count("Coolant") >= 2


@respx.mock
async def test_pi_detail_404_for_unknown_planet(
    client: TestClient,
    test_settings: Settings,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair, scopes=[PI_SCOPE])
    _mock_colonies(test_settings)

    response = client.get("/pi/999999")

    assert response.status_code == 404


@respx.mock
async def test_pi_list_shows_scope_notice_when_scope_missing(
    client: TestClient,
    test_settings: Settings,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair, scopes=[])

    response = client.get("/pi")

    assert response.status_code == 200
    assert "extra permission" in response.text
    assert "/auth/logout" in response.text
    assert not any("/planets" in str(call.request.url) for call in respx.calls)


@respx.mock
async def test_pi_list_shows_scope_notice_on_esi_403(
    client: TestClient,
    test_settings: Settings,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair, scopes=[PI_SCOPE])
    respx.get(f"{test_settings.esi_base_url}/characters/{CHARACTER_ID}/planets/").mock(
        return_value=Response(403, json={"error": "missing scope"})
    )

    response = client.get("/pi")

    assert response.status_code == 200
    assert "extra permission" in response.text


async def test_pi_list_requires_login(client: TestClient) -> None:
    response = client.get("/pi", follow_redirects=False)

    assert response.status_code == 401
