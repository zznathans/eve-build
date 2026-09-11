import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from httpx import Response
from mongomock_motor import AsyncMongoMockClient

from app.core.config import Settings
from tests.test_blueprints_routes import CHARACTER_ID, _log_in
from tests.test_build_routes import (
    COMPONENT_TYPE_ID,
    SHIP_BLUEPRINT_TYPE_ID,
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


WATER_TYPE_ID = 3645  # a P1 planetary commodity - see sde.PLANETARY_MATERIAL_CATEGORY_IDS


async def _seed_ship_with_pi_material(mongo_db: AsyncMongoMockClient) -> None:
    await mongo_db.sde_types.insert_many(
        [
            {"_id": SHIP_TYPE_ID, "name": "Test Ship", "published": True},
            {"_id": TRITANIUM_TYPE_ID, "name": "Tritanium", "published": True},
            {"_id": WATER_TYPE_ID, "name": "Water", "published": True, "category_id": 43},
        ]
    )
    await mongo_db.sde_blueprints.insert_one(
        {
            "_id": SHIP_BLUEPRINT_TYPE_ID,
            "product_type_id": SHIP_TYPE_ID,
            "product_quantity": 1,
            "materials": [
                {"type_id": TRITANIUM_TYPE_ID, "quantity": 100},
                {"type_id": WATER_TYPE_ID, "quantity": 20},
            ],
            "activity_id": 1,
        }
    )


def _mock_assets(settings: Settings, assets: list[dict[str, object]] | None = None) -> None:
    """Mocks the plan detail page's owned-assets lookup (used for the Total Bill of
    Materials availability gauge) - defaults to owning nothing, since most plan tests
    don't care about asset availability."""
    respx.get(f"{settings.esi_base_url}/characters/{CHARACTER_ID}/assets", params={"page": 1}).mock(
        return_value=Response(200, headers={"X-Pages": "1"}, json=assets or [])
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
async def test_update_job_quantity_requires_login(client: TestClient) -> None:
    response = client.get("/plans/some-plan/jobs/some-job/update", params={"qty": 3})

    assert response.status_code == 401


@respx.mock
async def test_update_job_quantity_changes_the_job_and_redirects_back(
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
    doc = await mongo_db.plans.find_one({"_id": plan_id})
    assert doc is not None
    job_id = doc["jobs"][0]["job_id"]

    response = client.get(
        f"/plans/{plan_id}/jobs/{job_id}/update", params={"qty": 7}, follow_redirects=False
    )

    assert response.status_code in (302, 303, 307)
    assert response.headers["location"] == f"/plans/{plan_id}"
    updated = await mongo_db.plans.find_one({"_id": plan_id})
    assert updated is not None
    assert updated["jobs"][0]["target_quantity"] == 7


@respx.mock
async def test_update_job_quantity_404s_for_unknown_job(
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

    response = client.get(f"/plans/{plan_id}/jobs/nonexistent/update", params={"qty": 7})

    assert response.status_code == 404


@respx.mock
async def test_set_job_build_flag_requires_login(client: TestClient) -> None:
    response = client.get(
        "/plans/some-plan/jobs/some-job/build-set", params={"type_id": 1, "build": "true"}
    )

    assert response.status_code == 401


@respx.mock
async def test_set_job_build_flag_true_expands_the_material_into_a_build_step(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    _mock_assets(test_settings)
    await _seed_two_level_ship(mongo_db)

    create_response = client.get(
        "/plans/create", params={"type_id": SHIP_TYPE_ID, "qty": 1}, follow_redirects=False
    )
    plan_id = create_response.headers["location"].removeprefix("/plans/")
    doc = await mongo_db.plans.find_one({"_id": plan_id})
    assert doc is not None
    job_id = doc["jobs"][0]["job_id"]

    response = client.get(
        f"/plans/{plan_id}/jobs/{job_id}/build-set",
        params={"type_id": COMPONENT_TYPE_ID, "build": "true"},
        follow_redirects=False,
    )

    assert response.status_code in (302, 303, 307)
    assert response.headers["location"] == f"/plans/{plan_id}"
    updated = await mongo_db.plans.find_one({"_id": plan_id})
    assert updated is not None
    assert updated["jobs"][0]["build_set"] == [COMPONENT_TYPE_ID]

    detail_response = client.get(f"/plans/{plan_id}")
    assert detail_response.status_code == 200
    assert "Test Component" in detail_response.text


@respx.mock
async def test_set_job_build_flag_false_collapses_the_step_back_to_a_material(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    await _seed_two_level_ship(mongo_db)

    create_response = client.get(
        "/plans/create",
        params={"type_id": SHIP_TYPE_ID, "qty": 1, "build": str(COMPONENT_TYPE_ID)},
        follow_redirects=False,
    )
    plan_id = create_response.headers["location"].removeprefix("/plans/")
    doc = await mongo_db.plans.find_one({"_id": plan_id})
    assert doc is not None
    job_id = doc["jobs"][0]["job_id"]

    response = client.get(
        f"/plans/{plan_id}/jobs/{job_id}/build-set",
        params={"type_id": COMPONENT_TYPE_ID, "build": "false"},
        follow_redirects=False,
    )

    assert response.status_code in (302, 303, 307)
    updated = await mongo_db.plans.find_one({"_id": plan_id})
    assert updated is not None
    assert updated["jobs"][0]["build_set"] == []


@respx.mock
async def test_set_job_build_flag_404s_for_unknown_job(
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

    response = client.get(
        f"/plans/{plan_id}/jobs/nonexistent/build-set",
        params={"type_id": TRITANIUM_TYPE_ID, "build": "true"},
    )

    assert response.status_code == 404


@respx.mock
async def test_remove_job_requires_login(client: TestClient) -> None:
    response = client.get("/plans/some-plan/jobs/some-job/delete")

    assert response.status_code == 401


@respx.mock
async def test_remove_job_deletes_it_and_redirects_back(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    _mock_assets(test_settings)
    await _seed_buildable_ship(mongo_db)
    await _seed_buildable_module(mongo_db)

    create_response = client.get(
        "/plans/create", params={"type_id": SHIP_TYPE_ID, "qty": 1}, follow_redirects=False
    )
    plan_id = create_response.headers["location"].removeprefix("/plans/")
    client.get(f"/plans/{plan_id}/add-job", params={"type_id": MODULE_TYPE_ID, "qty": 1})
    doc = await mongo_db.plans.find_one({"_id": plan_id})
    assert doc is not None
    module_job_id = doc["jobs"][1]["job_id"]

    response = client.get(f"/plans/{plan_id}/jobs/{module_job_id}/delete", follow_redirects=False)

    assert response.status_code in (302, 303, 307)
    assert response.headers["location"] == f"/plans/{plan_id}"
    updated = await mongo_db.plans.find_one({"_id": plan_id})
    assert updated is not None
    assert len(updated["jobs"]) == 1
    assert updated["jobs"][0]["target_type_id"] == SHIP_TYPE_ID


@respx.mock
async def test_remove_job_400s_for_the_plans_only_job(
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
    doc = await mongo_db.plans.find_one({"_id": plan_id})
    assert doc is not None
    job_id = doc["jobs"][0]["job_id"]

    response = client.get(f"/plans/{plan_id}/jobs/{job_id}/delete")

    assert response.status_code == 400
    unchanged = await mongo_db.plans.find_one({"_id": plan_id})
    assert unchanged is not None
    assert len(unchanged["jobs"]) == 1


@respx.mock
async def test_remove_job_404s_for_unknown_plan(
    client: TestClient,
    test_settings: Settings,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)

    response = client.get("/plans/nonexistent/jobs/some-job/delete")

    assert response.status_code == 404


@respx.mock
async def test_plan_detail_hides_remove_button_for_the_only_job(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    _mock_assets(test_settings)
    await _seed_buildable_ship(mongo_db)

    create_response = client.get(
        "/plans/create", params={"type_id": SHIP_TYPE_ID, "qty": 1}, follow_redirects=False
    )
    plan_id = create_response.headers["location"].removeprefix("/plans/")

    response = client.get(f"/plans/{plan_id}")

    assert response.status_code == 200
    assert "Remove" not in response.text


@respx.mock
async def test_plan_detail_shows_remove_button_for_each_job_when_multiple_exist(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    _mock_assets(test_settings)
    await _seed_buildable_ship(mongo_db)
    await _seed_buildable_module(mongo_db)

    create_response = client.get(
        "/plans/create", params={"type_id": SHIP_TYPE_ID, "qty": 1}, follow_redirects=False
    )
    plan_id = create_response.headers["location"].removeprefix("/plans/")
    client.get(f"/plans/{plan_id}/add-job", params={"type_id": MODULE_TYPE_ID, "qty": 1})

    response = client.get(f"/plans/{plan_id}")

    assert response.status_code == 200
    assert response.text.count(f"/plans/{plan_id}/jobs/") >= 4  # update + delete, per job


@respx.mock
async def test_plan_detail_shows_editable_quantity_for_each_job(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    _mock_assets(test_settings)
    await _seed_buildable_ship(mongo_db)

    create_response = client.get(
        "/plans/create", params={"type_id": SHIP_TYPE_ID, "qty": 3}, follow_redirects=False
    )
    plan_id = create_response.headers["location"].removeprefix("/plans/")
    doc = await mongo_db.plans.find_one({"_id": plan_id})
    assert doc is not None
    job_id = doc["jobs"][0]["job_id"]

    response = client.get(f"/plans/{plan_id}")

    assert response.status_code == 200
    assert f'action="/plans/{plan_id}/jobs/{job_id}/update"' in response.text
    assert 'value="3"' in response.text


@respx.mock
async def test_plan_detail_renders_a_single_job(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    _mock_assets(test_settings)
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
    assert (
        '<a class="btn btn-primary plan-header-action" href="/build/items?plan_id=' in response.text
    )


@respx.mock
async def test_plan_detail_aggregates_totals_and_materials_across_jobs(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    _mock_assets(
        test_settings,
        [
            {
                "item_id": 1,
                "type_id": TRITANIUM_TYPE_ID,
                "location_id": 60003760,
                "location_flag": "Hangar",
                "location_type": "station",
                "quantity": 50,
                "is_singleton": False,
            }
        ],
    )
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
    # Total Bill of Materials panel merges both jobs' Tritanium demand: 100 + 100 = 200.
    assert "Total Bill of Materials" in response.text
    assert response.text.count("Tritanium") >= 3  # once per job card, plus the combined panel
    assert "<td>200</td>" in response.text
    # Owns 50 of the 200 needed - a 25% availability gauge on the combined panel, with
    # owned to the left of the bar and needed to the right.
    assert '<span class="mini-gauge-text mini-gauge-text-left">50</span>' in response.text
    assert 'style="width: 25%' in response.text
    assert '<span class="mini-gauge-text">200</span>' in response.text


@respx.mock
async def test_plan_detail_splits_pi_materials_into_their_own_section(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    _mock_assets(test_settings)
    await _seed_ship_with_pi_material(mongo_db)

    create_response = client.get(
        "/plans/create", params={"type_id": SHIP_TYPE_ID, "qty": 1}, follow_redirects=False
    )
    plan_id = create_response.headers["location"].removeprefix("/plans/")

    response = client.get(f"/plans/{plan_id}")

    assert response.status_code == 200
    assert "Total Planetary Materials" in response.text
    assert "Planetary Materials" in response.text  # per-job subhead
    # Water only appears in the planetary sections, Tritanium only in the regular ones -
    # each shows up once per job card and once in its combined panel.
    assert response.text.count("Water") == 2
    assert response.text.count("Tritanium") == 2


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


@respx.mock
async def test_delete_plan_requires_login(client: TestClient) -> None:
    response = client.get("/plans/some-plan/delete")

    assert response.status_code == 401


@respx.mock
async def test_delete_plan_deletes_it_and_redirects_to_the_list(
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

    response = client.get(f"/plans/{plan_id}/delete", follow_redirects=False)

    assert response.status_code in (302, 303, 307)
    assert response.headers["location"] == "/plans"
    assert await mongo_db.plans.find_one({"_id": plan_id}) is None


@respx.mock
async def test_delete_plan_404s_for_unknown_plan(
    client: TestClient,
    test_settings: Settings,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)

    response = client.get("/plans/nonexistent/delete")

    assert response.status_code == 404


@respx.mock
async def test_delete_plan_404s_for_a_different_owners_plan(
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

    response = client.get("/plans/someone-elses-plan/delete")

    assert response.status_code == 404
    assert await mongo_db.plans.find_one({"_id": "someone-elses-plan"}) is not None


@respx.mock
async def test_plan_detail_shows_a_delete_plan_button(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    _mock_assets(test_settings)
    await _seed_buildable_ship(mongo_db)

    create_response = client.get(
        "/plans/create", params={"type_id": SHIP_TYPE_ID, "qty": 1}, follow_redirects=False
    )
    plan_id = create_response.headers["location"].removeprefix("/plans/")

    response = client.get(f"/plans/{plan_id}")

    assert response.status_code == 200
    assert f'href="/plans/{plan_id}/delete"' in response.text
