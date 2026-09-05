import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from app.core.config import Settings
from tests.test_blueprints_routes import CHARACTER_ID, _log_in
from tests.test_build_routes import (
    COMPONENT_TYPE_ID,
    SHIP_TYPE_ID,
    TRITANIUM_TYPE_ID,
    _seed_buildable_ship,
    _seed_two_level_ship,
)

MODULE_TYPE_ID = 700
MODULE_BLUEPRINT_TYPE_ID = 701


async def _seed_buildable_module(mongo_db: AsyncMongoMockClient) -> None:
    await mongo_db.sde_types.insert_one(
        {"_id": MODULE_TYPE_ID, "name": "Test Module", "published": True}
    )
    await mongo_db.sde_blueprints.insert_one(
        {
            "_id": MODULE_BLUEPRINT_TYPE_ID,
            "product_type_id": MODULE_TYPE_ID,
            "product_quantity": 1,
            "materials": [{"type_id": TRITANIUM_TYPE_ID, "quantity": 50}],
            "activity_id": 1,
        }
    )


@respx.mock
async def test_plans_create_requires_login(client: TestClient) -> None:
    response = client.get("/plans/create", params={"type_id": SHIP_TYPE_ID})

    assert response.status_code == 401


@respx.mock
async def test_plans_create_saves_and_redirects(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    await _seed_two_level_ship(mongo_db)

    response = client.get(
        "/plans/create",
        params={"type_id": SHIP_TYPE_ID, "qty": 2, "build": str(COMPONENT_TYPE_ID)},
        follow_redirects=False,
    )

    assert response.status_code in (302, 303, 307)
    location = response.headers["location"]
    assert location.startswith("/plans/")
    plan_id = location.removeprefix("/plans/")

    doc = await mongo_db.plans.find_one({"_id": plan_id})
    assert doc is not None
    assert doc["character_id"] == CHARACTER_ID
    assert len(doc["jobs"]) == 1
    job = doc["jobs"][0]
    assert job["target_type_id"] == SHIP_TYPE_ID
    assert job["target_quantity"] == 2
    assert job["build_set"] == [COMPONENT_TYPE_ID]


@respx.mock
async def test_add_job_requires_login(client: TestClient) -> None:
    response = client.get("/plans/some-plan/add-job", params={"type_id": SHIP_TYPE_ID})

    assert response.status_code == 401


@respx.mock
async def test_add_job_appends_and_redirects_back_to_the_plan(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    await _seed_buildable_ship(mongo_db)
    await _seed_buildable_module(mongo_db)

    create_response = client.get(
        "/plans/create", params={"type_id": SHIP_TYPE_ID, "qty": 1}, follow_redirects=False
    )
    plan_id = create_response.headers["location"].removeprefix("/plans/")

    response = client.get(
        f"/plans/{plan_id}/add-job",
        params={"type_id": MODULE_TYPE_ID, "qty": 4},
        follow_redirects=False,
    )

    assert response.status_code in (302, 303, 307)
    assert response.headers["location"] == f"/plans/{plan_id}"

    doc = await mongo_db.plans.find_one({"_id": plan_id})
    assert doc is not None
    assert len(doc["jobs"]) == 2
    assert doc["jobs"][1]["target_type_id"] == MODULE_TYPE_ID
    assert doc["jobs"][1]["target_quantity"] == 4


@respx.mock
async def test_add_job_404s_for_unknown_plan(
    client: TestClient,
    test_settings: Settings,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)

    response = client.get("/plans/nonexistent/add-job", params={"type_id": SHIP_TYPE_ID})

    assert response.status_code == 404


@respx.mock
async def test_add_job_404s_for_a_different_owners_plan(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    await mongo_db.plans.insert_one(
        {
            "_id": "someone-elses-plan",
            "character_id": CHARACTER_ID + 1,
            "jobs": [],
            "created_at": None,
            "updated_at": None,
        }
    )
    _log_in(client, test_settings, rsa_key_pair)

    response = client.get("/plans/someone-elses-plan/add-job", params={"type_id": SHIP_TYPE_ID})

    assert response.status_code == 404


@respx.mock
async def test_plan_detail_renders_a_single_job(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    await _seed_buildable_ship(mongo_db)

    create_response = client.get(
        "/plans/create", params={"type_id": SHIP_TYPE_ID, "qty": 1}, follow_redirects=False
    )
    plan_id = create_response.headers["location"].removeprefix("/plans/")

    response = client.get(f"/plans/{plan_id}")

    assert response.status_code == 200
    assert "Test Ship" in response.text
    assert "Tritanium" in response.text
    assert '<div class="label">Jobs</div>' in response.text
    assert '<div class="value">1</div>' in response.text
    assert '<a class="btn btn-primary" href="/build/items?plan_id=' in response.text


@respx.mock
async def test_plan_detail_aggregates_totals_and_materials_across_jobs(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    await _seed_buildable_ship(mongo_db)  # Ship needs 100 Tritanium
    await _seed_buildable_module(mongo_db)  # Module needs 50 Tritanium/run

    create_response = client.get(
        "/plans/create", params={"type_id": SHIP_TYPE_ID, "qty": 1}, follow_redirects=False
    )
    plan_id = create_response.headers["location"].removeprefix("/plans/")
    client.get(
        f"/plans/{plan_id}/add-job", params={"type_id": MODULE_TYPE_ID, "qty": 2}
    )  # 2 runs * 50 = 100 Tritanium

    response = client.get(f"/plans/{plan_id}")

    assert response.status_code == 200
    assert '<div class="value">2</div>' in response.text  # Jobs tile
    assert "Test Ship" in response.text
    assert "Test Module" in response.text
    # Materials Needed panel merges both jobs' Tritanium demand: 100 + 100 = 200.
    assert "Materials Needed" in response.text
    assert response.text.count("Tritanium") >= 3  # once per job card, plus the combined panel
    assert "<td>200</td>" in response.text


@respx.mock
async def test_plan_detail_404s_for_unknown_id(
    client: TestClient,
    test_settings: Settings,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)

    response = client.get("/plans/nonexistent")

    assert response.status_code == 404


@respx.mock
async def test_plan_detail_404s_for_a_different_owners_plan(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    await _seed_buildable_ship(mongo_db)
    await mongo_db.plans.insert_one(
        {
            "_id": "someone-elses-plan",
            "character_id": CHARACTER_ID + 1,
            "jobs": [
                {
                    "job_id": "job-1",
                    "target_type_id": SHIP_TYPE_ID,
                    "target_quantity": 1,
                    "build_set": [],
                }
            ],
            "created_at": None,
            "updated_at": None,
        }
    )
    _log_in(client, test_settings, rsa_key_pair)

    response = client.get("/plans/someone-elses-plan")

    assert response.status_code == 404


@respx.mock
async def test_plans_list_requires_login(client: TestClient) -> None:
    response = client.get("/plans")

    assert response.status_code == 401


@respx.mock
async def test_plans_list_shows_empty_message(
    client: TestClient,
    test_settings: Settings,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)

    response = client.get("/plans")

    assert response.status_code == 200
    assert "No plans saved yet" in response.text


@respx.mock
async def test_plans_list_shows_saved_plans_with_links(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    await _seed_buildable_ship(mongo_db)

    create_response = client.get(
        "/plans/create", params={"type_id": SHIP_TYPE_ID, "qty": 5}, follow_redirects=False
    )
    plan_id = create_response.headers["location"].removeprefix("/plans/")

    response = client.get("/plans")

    assert response.status_code == 200
    assert "Test Ship" in response.text
    assert "1 job" in response.text
    assert f'href="/plans/{plan_id}"' in response.text
