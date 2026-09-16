import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from httpx import Response
from mongomock_motor import AsyncMongoMockClient

from app.core.config import Settings
from tests.test_blueprints_routes import CHARACTER_ID, _log_in


def test_read_root_logged_out(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Log in with EVE Online" in response.text


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@respx.mock
async def test_dashboard_shows_summary_counts(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    await mongo_db.sde_types.insert_one(
        {"_id": 588, "name": "Rifter Blueprint", "group_id": 1, "published": True, "tech_level": 1}
    )

    respx.get(
        f"{test_settings.esi_base_url}/characters/{CHARACTER_ID}/blueprints", params={"page": 1}
    ).mock(
        return_value=Response(
            200,
            headers={"X-Pages": "1"},
            json=[
                {
                    "item_id": 1,
                    "type_id": 588,
                    "location_id": 60003760,
                    "location_flag": "Hangar",
                    "quantity": -1,
                    "runs": -1,
                    "material_efficiency": 0,
                    "time_efficiency": 0,
                }
            ],
        )
    )
    respx.get(f"{test_settings.esi_base_url}/universe/stations/60003760").mock(
        return_value=Response(200, json={"name": "Jita IV - Moon 4"})
    )
    respx.get(f"{test_settings.esi_base_url}/characters/{CHARACTER_ID}/industry/jobs").mock(
        return_value=Response(
            200,
            json=[
                {
                    "job_id": 1,
                    "installer_id": CHARACTER_ID,
                    "facility_id": 60003760,
                    "station_id": 60003760,
                    "activity_id": 1,
                    "blueprint_id": 100,
                    "blueprint_type_id": 588,
                    "blueprint_location_id": 60003760,
                    "output_location_id": 60003760,
                    "runs": 1,
                    "status": "active",
                    "duration": 1200,
                    "start_date": "2026-01-01T00:00:00Z",
                    "end_date": "2026-01-01T01:00:00Z",
                },
                {
                    "job_id": 2,
                    "installer_id": CHARACTER_ID,
                    "facility_id": 60003760,
                    "station_id": 60003760,
                    "activity_id": 1,
                    "blueprint_id": 101,
                    "blueprint_type_id": 589,
                    "blueprint_location_id": 60003760,
                    "output_location_id": 60003760,
                    "runs": 1,
                    "status": "delivered",
                    "duration": 1200,
                    "start_date": "2026-01-01T00:00:00Z",
                    "end_date": "2026-01-01T01:00:00Z",
                },
            ],
        )
    )

    response = client.get("/")

    assert response.status_code == 200
    assert ">1<" in response.text  # 1 blueprint
    assert "Manufacturing" in response.text
    assert "mini-gauge" in response.text
    assert "Rifter Blueprint" in response.text
    assert "https://images.evetech.net/types/588/bp" in response.text
    assert "Jita IV - Moon 4" in response.text
    assert 'href="/jobs/1"' in response.text
    # only the "active" job counts as running, not the "delivered" one
    assert response.text.count("Manufacturing") == 1
    assert "No running industry jobs." not in response.text
