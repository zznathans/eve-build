import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from httpx import Response
from mongomock_motor import AsyncMongoMockClient

from app.core.config import Settings
from app.web import item_icon_url
from tests.fixtures_ship_chain import (
    COMPONENT_TYPE_ID,
    SHIP_BLUEPRINT_TYPE_ID,
    SHIP_TYPE_ID,
    TRITANIUM_TYPE_ID,
    _seed_buildable_ship,
    _seed_two_level_ship,
)
from tests.test_blueprints_routes import CHARACTER_ID, _log_in

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


def _mock_blueprints(settings: Settings, blueprints: list[dict[str, object]]) -> None:
    respx.get(
        f"{settings.esi_base_url}/characters/{CHARACTER_ID}/blueprints", params={"page": 1}
    ).mock(return_value=Response(200, headers={"X-Pages": "1"}, json=blueprints))


def _blueprint_entry(item_id: int, type_id: int) -> dict[str, object]:
    return {
        "item_id": item_id,
        "type_id": type_id,
        "location_id": 60003760,
        "location_flag": "Hangar",
        "quantity": -1,
        "runs": -1,
        "material_efficiency": 10,
        "time_efficiency": 20,
    }


@respx.mock
async def test_plans_create_requires_login(client: TestClient) -> None:
    response = client.get("/plans/create", params={"type_id": SHIP_TYPE_ID})

    assert response.status_code == 401


@respx.mock
async def test_new_plan_requires_login(client: TestClient) -> None:
    response = client.get("/plans/new")

    assert response.status_code == 401


@respx.mock
async def test_new_plan_creates_an_empty_plan_and_redirects_to_the_picker(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)

    response = client.get("/plans/new", follow_redirects=False)

    assert response.status_code in (302, 303, 307)
    location = response.headers["location"]
    plan_id = location.removeprefix("/plans/").removesuffix("/add-from-blueprints")
    assert location == f"/plans/{plan_id}/add-from-blueprints"

    doc = await mongo_db.plans.find_one({"_id": plan_id})
    assert doc is not None
    assert doc["character_id"] == CHARACTER_ID
    assert doc["jobs"] == []


@respx.mock
async def test_new_plan_detail_shows_empty_state_and_add_from_blueprints_button(
    client: TestClient,
    test_settings: Settings,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    _mock_assets(test_settings)

    new_response = client.get("/plans/new", follow_redirects=False)
    plan_id = (
        new_response.headers["location"]
        .removesuffix("/add-from-blueprints")
        .removeprefix("/plans/")
    )

    response = client.get(f"/plans/{plan_id}")

    assert response.status_code == 200
    assert "This plan has no jobs yet" in response.text
    assert f'href="/plans/{plan_id}/add-from-blueprints"' in response.text


@respx.mock
async def test_plans_list_shows_a_new_plan_button(
    client: TestClient,
    test_settings: Settings,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)

    response = client.get("/plans")

    assert response.status_code == 200
    assert 'href="/plans/new"' in response.text


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
async def test_add_from_blueprints_requires_login(client: TestClient) -> None:
    response = client.get("/plans/some-plan/add-from-blueprints")

    assert response.status_code == 401


@respx.mock
async def test_add_from_blueprints_lists_owned_blueprints_with_checkboxes(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    await _seed_buildable_ship(mongo_db)
    # _seed_buildable_ship only seeds SDE type docs for the product/materials, not the
    # blueprint type itself - the blueprints list (like the real /blueprints page) shows
    # the blueprint's own name, so it needs its own sde_types doc.
    await mongo_db.sde_types.insert_one(
        {"_id": SHIP_BLUEPRINT_TYPE_ID, "name": "Test Ship Blueprint", "published": True}
    )
    _mock_blueprints(test_settings, [_blueprint_entry(1001, SHIP_BLUEPRINT_TYPE_ID)])

    create_response = client.get(
        "/plans/create", params={"type_id": SHIP_TYPE_ID, "qty": 1}, follow_redirects=False
    )
    plan_id = create_response.headers["location"].removeprefix("/plans/")

    response = client.get(f"/plans/{plan_id}/add-from-blueprints")

    assert response.status_code == 200
    assert "Test Ship Blueprint" in response.text
    assert (
        '<input class="bp-checkbox" type="checkbox" name="item_id" value="1001">' in response.text
    )


@respx.mock
async def test_add_from_blueprints_filters_by_search(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    await _seed_buildable_ship(mongo_db)
    await _seed_buildable_module(mongo_db)
    await mongo_db.sde_types.insert_many(
        [
            {"_id": SHIP_BLUEPRINT_TYPE_ID, "name": "Test Ship Blueprint", "published": True},
            {"_id": MODULE_BLUEPRINT_TYPE_ID, "name": "Test Module Blueprint", "published": True},
        ]
    )
    _mock_blueprints(
        test_settings,
        [
            _blueprint_entry(1001, SHIP_BLUEPRINT_TYPE_ID),
            _blueprint_entry(1002, MODULE_BLUEPRINT_TYPE_ID),
        ],
    )

    create_response = client.get(
        "/plans/create", params={"type_id": SHIP_TYPE_ID, "qty": 1}, follow_redirects=False
    )
    plan_id = create_response.headers["location"].removeprefix("/plans/")

    response = client.get(f"/plans/{plan_id}/add-from-blueprints", params={"search": "ship"})

    assert response.status_code == 200
    assert "Test Ship Blueprint" in response.text
    assert "Test Module Blueprint" not in response.text
    assert 'value="ship"' in response.text


@respx.mock
async def test_add_from_blueprints_shows_no_match_message_for_search_with_no_hits(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    await _seed_buildable_ship(mongo_db)
    await mongo_db.sde_types.insert_one(
        {"_id": SHIP_BLUEPRINT_TYPE_ID, "name": "Test Ship Blueprint", "published": True}
    )
    _mock_blueprints(test_settings, [_blueprint_entry(1001, SHIP_BLUEPRINT_TYPE_ID)])

    create_response = client.get(
        "/plans/create", params={"type_id": SHIP_TYPE_ID, "qty": 1}, follow_redirects=False
    )
    plan_id = create_response.headers["location"].removeprefix("/plans/")

    response = client.get(
        f"/plans/{plan_id}/add-from-blueprints", params={"search": "nonexistent-item"}
    )

    assert response.status_code == 200
    assert "No blueprints match your search." in response.text


@respx.mock
async def test_add_from_blueprints_404s_for_unknown_plan(
    client: TestClient,
    test_settings: Settings,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)

    response = client.get("/plans/nonexistent/add-from-blueprints")

    assert response.status_code == 404


@respx.mock
async def test_add_jobs_from_blueprints_requires_login(client: TestClient) -> None:
    response = client.get("/plans/some-plan/add-from-blueprints/add", params={"item_id": 1001})

    assert response.status_code == 401


@respx.mock
async def test_add_jobs_from_blueprints_adds_selected_jobs_and_redirects(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    _mock_assets(test_settings)
    await _seed_buildable_ship(mongo_db)
    await _seed_buildable_module(mongo_db)
    _mock_blueprints(
        test_settings,
        [
            _blueprint_entry(1001, SHIP_BLUEPRINT_TYPE_ID),
            _blueprint_entry(1002, MODULE_BLUEPRINT_TYPE_ID),
        ],
    )

    create_response = client.get(
        "/plans/create", params={"type_id": SHIP_TYPE_ID, "qty": 1}, follow_redirects=False
    )
    plan_id = create_response.headers["location"].removeprefix("/plans/")

    response = client.get(
        f"/plans/{plan_id}/add-from-blueprints/add",
        params={"item_id": [1001, 1002]},
        follow_redirects=False,
    )

    assert response.status_code in (302, 303, 307)
    assert response.headers["location"] == f"/plans/{plan_id}"
    doc = await mongo_db.plans.find_one({"_id": plan_id})
    assert doc is not None
    assert len(doc["jobs"]) == 3  # the plan's original job, plus ship + module added here
    added_target_type_ids = {job["target_type_id"] for job in doc["jobs"][1:]}
    assert added_target_type_ids == {SHIP_TYPE_ID, MODULE_TYPE_ID}
    added_by_target = {job["target_type_id"]: job for job in doc["jobs"][1:]}
    assert added_by_target[SHIP_TYPE_ID]["blueprint_item_id"] == 1001
    assert added_by_target[MODULE_TYPE_ID]["blueprint_item_id"] == 1002


@respx.mock
async def test_add_jobs_from_blueprints_defaults_quantity_to_runs_remaining_for_a_copy(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    await _seed_buildable_ship(mongo_db)
    await _seed_buildable_module(mongo_db)
    copy_entry = {**_blueprint_entry(1001, MODULE_BLUEPRINT_TYPE_ID), "quantity": -2, "runs": 5}
    _mock_blueprints(test_settings, [copy_entry])

    create_response = client.get(
        "/plans/create", params={"type_id": SHIP_TYPE_ID, "qty": 1}, follow_redirects=False
    )
    plan_id = create_response.headers["location"].removeprefix("/plans/")

    client.get(
        f"/plans/{plan_id}/add-from-blueprints/add",
        params={"item_id": 1001},
        follow_redirects=False,
    )

    doc = await mongo_db.plans.find_one({"_id": plan_id})
    assert doc is not None
    added_job = doc["jobs"][1]
    # Module produces 1/run and the copy has 5 runs left -> default to using them all.
    assert added_job["target_quantity"] == 5


@respx.mock
async def test_add_jobs_from_blueprints_ignores_unselected_and_unowned_ids(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    await _seed_buildable_ship(mongo_db)
    _mock_blueprints(test_settings, [_blueprint_entry(1001, SHIP_BLUEPRINT_TYPE_ID)])

    create_response = client.get(
        "/plans/create", params={"type_id": SHIP_TYPE_ID, "qty": 1}, follow_redirects=False
    )
    plan_id = create_response.headers["location"].removeprefix("/plans/")

    response = client.get(
        f"/plans/{plan_id}/add-from-blueprints/add", follow_redirects=False
    )  # no item_id selected

    assert response.status_code in (302, 303, 307)
    doc = await mongo_db.plans.find_one({"_id": plan_id})
    assert doc is not None
    assert len(doc["jobs"]) == 1


@respx.mock
async def test_add_jobs_from_blueprints_404s_for_unknown_plan(
    client: TestClient,
    test_settings: Settings,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)

    response = client.get("/plans/nonexistent/add-from-blueprints/add", params={"item_id": 1001})

    assert response.status_code == 404


@respx.mock
async def test_plan_detail_shows_blueprint_link_reduced_materials_and_build_time(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    _mock_assets(test_settings)
    await _seed_buildable_ship(mongo_db)
    await mongo_db.sde_types.insert_one(
        {"_id": SHIP_BLUEPRINT_TYPE_ID, "name": "Test Ship Blueprint", "published": True}
    )
    await mongo_db.sde_blueprints.update_one(
        {"_id": SHIP_BLUEPRINT_TYPE_ID}, {"$set": {"manufacturing_time_seconds": 1000}}
    )
    # 10% ME, 20% TE.
    _mock_blueprints(test_settings, [_blueprint_entry(1001, SHIP_BLUEPRINT_TYPE_ID)])

    create_response = client.get(
        "/plans/create", params={"type_id": SHIP_TYPE_ID, "qty": 1}, follow_redirects=False
    )
    plan_id = create_response.headers["location"].removeprefix("/plans/")
    client.get(
        f"/plans/{plan_id}/add-from-blueprints/add",
        params={"item_id": 1001},
        follow_redirects=False,
    )
    doc = await mongo_db.plans.find_one({"_id": plan_id})
    assert doc is not None
    job_id = doc["jobs"][1]["job_id"]

    response = client.get(f"/plans/{plan_id}")

    assert response.status_code == 200
    assert 'href="/blueprints/1001"' in response.text
    assert "Test Ship Blueprint" in response.text
    assert f'action="/plans/{plan_id}/jobs/{job_id}/update"' in response.text
    # Base recipe needs 100 Tritanium/run; 10% ME -> 90.
    assert "<td>90</td>" in response.text
    # 1 run * 1000s * (1 - 20%) = 800s -> 13m.
    assert "13m" in response.text
    # The linked blueprint's own ME/TE are shown on the job panel too.
    assert "10/10" in response.text
    assert "20/20" in response.text


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
        f'<a class="btn btn-primary plan-header-action" '
        f'href="/plans/{plan_id}/add-from-blueprints">' in response.text
    )
    assert "<th>Item</th>" in response.text
    assert "<th>Quantity</th>" in response.text
    assert "<th>Value</th>" in response.text
    assert "<th>Availability</th>" in response.text


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
async def test_plan_detail_shows_bulk_toggle_and_it_builds_the_material_in_every_job(
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
    client.get(f"/plans/{plan_id}/add-job", params={"type_id": SHIP_TYPE_ID, "qty": 2})

    # Component is bought (raw) in both jobs by default - the combined panel shows a
    # Build/Buy toggle group with Buy as the active (non-clickable) pill.
    response = client.get(f"/plans/{plan_id}")
    assert response.status_code == 200
    assert '<span class="flag flag-buy">Buy</span>' in response.text
    build_href = f"/plans/{plan_id}/materials/{COMPONENT_TYPE_ID}/build-set?build=true"
    assert f'href="{build_href}"' in response.text

    toggle_response = client.get(
        f"/plans/{plan_id}/materials/{COMPONENT_TYPE_ID}/build-set",
        params={"build": "true"},
        follow_redirects=False,
    )

    assert toggle_response.status_code in (302, 303, 307)
    assert toggle_response.headers["location"] == f"/plans/{plan_id}"
    doc = await mongo_db.plans.find_one({"_id": plan_id})
    assert doc is not None
    assert all(COMPONENT_TYPE_ID in job["build_set"] for job in doc["jobs"])


@respx.mock
async def test_plan_detail_shows_combined_build_steps_table_above_bill_of_materials(
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
    client.get(f"/plans/{plan_id}/add-job", params={"type_id": SHIP_TYPE_ID, "qty": 2})

    response = client.get(f"/plans/{plan_id}")

    assert response.status_code == 200
    steps_pos = response.text.index("Total Build Steps")
    materials_pos = response.text.index("Total Bill of Materials")
    assert steps_pos < materials_pos  # steps table renders above the materials table
    # Ship is a target in both jobs (1 + 2 = 3 runs combined) - no Buy toggle for it, since
    # resolve_build_chain always expands a job's own target regardless of build_set.
    assert "<td>3</td>" in response.text
    ship_row_start = response.text.index("Test Ship", steps_pos)
    ship_row_end = response.text.index("</tr>", ship_row_start)
    assert "flag-buy-toggle" not in response.text[ship_row_start:ship_row_end]
    assert "flag-toggle-group" not in response.text[ship_row_start:ship_row_end]

    # Component isn't built anywhere yet, so it doesn't appear as a step at all.
    assert "Test Component" not in response.text[steps_pos:materials_pos]

    client.get(
        f"/plans/{plan_id}/materials/{COMPONENT_TYPE_ID}/build-set",
        params={"build": "true"},
        follow_redirects=False,
    )

    response2 = client.get(f"/plans/{plan_id}")
    assert response2.status_code == 200
    steps_pos2 = response2.text.index("Total Build Steps")
    materials_pos2 = response2.text.index("Total Bill of Materials")
    steps_section = response2.text[steps_pos2:materials_pos2]
    assert "Test Component" in steps_section
    # Now built everywhere - shown as the active (non-clickable) Build pill.
    assert '<span class="flag flag-build">Build</span>' in steps_section


@respx.mock
async def test_set_material_build_flag_for_all_jobs_requires_login(client: TestClient) -> None:
    response = client.get("/plans/some-plan/materials/500/build-set", params={"build": "true"})

    assert response.status_code == 401


@respx.mock
async def test_set_material_build_flag_for_all_jobs_404s_for_unknown_plan(
    client: TestClient,
    test_settings: Settings,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)

    response = client.get("/plans/nonexistent/materials/500/build-set", params={"build": "true"})

    assert response.status_code == 404


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
    assert "Test Ship" in response.text  # job icon tooltip, not the plan's own name
    assert "Untitled Plan" in response.text
    assert "1 job" in response.text
    assert f'href="/plans/{plan_id}"' in response.text


@respx.mock
async def test_plans_list_shows_an_icon_for_each_jobs_output_item(
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

    response = client.get("/plans")

    assert response.status_code == 200
    assert f'src="{item_icon_url(SHIP_TYPE_ID)}"' in response.text
    assert f'src="{item_icon_url(MODULE_TYPE_ID)}"' in response.text
    assert 'title="Test Module"' in response.text


@respx.mock
async def test_rename_plan_requires_login(client: TestClient) -> None:
    response = client.get("/plans/some-plan/rename", params={"name": "New Name"})

    assert response.status_code == 401


@respx.mock
async def test_rename_plan_sets_the_name_and_redirects_back(
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

    response = client.get(
        f"/plans/{plan_id}/rename",
        params={"name": "  Alpha Fleet Doctrine  "},
        follow_redirects=False,
    )

    assert response.status_code in (302, 303, 307)
    assert response.headers["location"] == f"/plans/{plan_id}"
    updated = await mongo_db.plans.find_one({"_id": plan_id})
    assert updated is not None
    assert updated["name"] == "Alpha Fleet Doctrine"  # whitespace trimmed

    detail_response = client.get(f"/plans/{plan_id}")
    assert 'value="Alpha Fleet Doctrine"' in detail_response.text


@respx.mock
async def test_rename_plan_404s_for_unknown_plan(
    client: TestClient,
    test_settings: Settings,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)

    response = client.get("/plans/nonexistent/rename", params={"name": "New Name"})

    assert response.status_code == 404


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
