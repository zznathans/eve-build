import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from app.core.config import Settings
from tests.test_blueprints_routes import _log_in

SHIP_TYPE_ID = 600
SHIP_BLUEPRINT_TYPE_ID = 601
TRITANIUM_TYPE_ID = 34
COMPONENT_TYPE_ID = 500
COMPONENT_BLUEPRINT_TYPE_ID = 501


async def _seed_buildable_ship(mongo_db: AsyncMongoMockClient) -> None:
    await mongo_db.sde_types.insert_many(
        [
            {"_id": SHIP_TYPE_ID, "name": "Test Ship", "published": True},
            {"_id": TRITANIUM_TYPE_ID, "name": "Tritanium", "published": True},
        ]
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


async def _seed_two_level_ship(mongo_db: AsyncMongoMockClient) -> None:
    await mongo_db.sde_types.insert_many(
        [
            {"_id": SHIP_TYPE_ID, "name": "Test Ship", "published": True},
            {"_id": COMPONENT_TYPE_ID, "name": "Test Component", "published": True},
            {"_id": TRITANIUM_TYPE_ID, "name": "Tritanium", "published": True},
        ]
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


def test_build_chooser_shows_both_options(client: TestClient) -> None:
    response = client.get("/build")

    assert response.status_code == 200
    assert "I know what I want to build" in response.text
    assert 'href="/build/items"' in response.text
    assert "I know which blueprint I want" in response.text
    assert 'href="/blueprints/catalog"' in response.text


def test_build_chooser_carries_plan_id_forward(client: TestClient) -> None:
    response = client.get("/build", params={"plan_id": "plan-123"})

    assert response.status_code == 200
    assert 'href="/build/items?plan_id=plan-123"' in response.text


async def test_item_search_finds_items_by_name(
    client: TestClient, mongo_db: AsyncMongoMockClient
) -> None:
    await _seed_buildable_ship(mongo_db)

    response = client.get("/build/items", params={"q": "test ship"})

    assert response.status_code == 200
    assert "Test Ship" in response.text
    assert f'href="/build/items/{SHIP_TYPE_ID}"' in response.text


async def test_item_search_carries_plan_id_forward(
    client: TestClient, mongo_db: AsyncMongoMockClient
) -> None:
    await _seed_buildable_ship(mongo_db)

    response = client.get("/build/items", params={"q": "test ship", "plan_id": "plan-123"})

    assert response.status_code == 200
    assert f'href="/build/items/{SHIP_TYPE_ID}?plan_id=plan-123"' in response.text
    assert '<input type="hidden" name="plan_id" value="plan-123">' in response.text


async def test_item_search_prompts_for_more_characters(client: TestClient) -> None:
    response = client.get("/build/items", params={"q": "a"})

    assert response.status_code == 200
    assert "Keep typing" in response.text


async def test_item_build_chain_shows_raw_materials(
    client: TestClient, mongo_db: AsyncMongoMockClient
) -> None:
    await _seed_buildable_ship(mongo_db)

    response = client.get(f"/build/items/{SHIP_TYPE_ID}")

    assert response.status_code == 200
    assert "Test Ship" in response.text
    assert "Tritanium" in response.text
    assert '<span class="item-value">100</span>' in response.text
    assert '<div class="value">1</div>' in response.text  # 1 build step
    assert 'href="/plans/create' not in response.text  # anonymous - no Add to Plan


@respx.mock
async def test_item_build_chain_shows_add_to_plan_when_logged_in(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    await _seed_buildable_ship(mongo_db)

    response = client.get(f"/build/items/{SHIP_TYPE_ID}", params={"qty": 2})

    assert response.status_code == 200
    href = f"/plans/create?type_id={SHIP_TYPE_ID}&amp;qty=2&amp;build="
    assert f'<a class="btn btn-primary" href="{href}">Add to Plan</a>' in response.text


@respx.mock
async def test_item_build_chain_add_to_plan_targets_add_job_when_plan_id_present(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    await _seed_buildable_ship(mongo_db)

    response = client.get(f"/build/items/{SHIP_TYPE_ID}", params={"qty": 2, "plan_id": "plan-123"})

    assert response.status_code == 200
    href = f"/plans/plan-123/add-job?type_id={SHIP_TYPE_ID}&amp;qty=2&amp;build="
    assert f'<a class="btn btn-primary" href="{href}">Add to Plan</a>' in response.text


async def test_item_build_chain_carries_plan_id_through_qty_form_and_toggles(
    client: TestClient, mongo_db: AsyncMongoMockClient
) -> None:
    await _seed_two_level_ship(mongo_db)

    response = client.get(f"/build/items/{SHIP_TYPE_ID}", params={"plan_id": "plan-123"})

    assert response.status_code == 200
    assert '<input type="hidden" name="plan_id" value="plan-123">' in response.text
    assert (
        f'href="/build/items/{SHIP_TYPE_ID}?qty=1&amp;build={COMPONENT_TYPE_ID}'
        '&amp;plan_id=plan-123"' in response.text
    )


async def test_item_build_chain_qty_form_preserves_build_state(
    client: TestClient, mongo_db: AsyncMongoMockClient
) -> None:
    await _seed_two_level_ship(mongo_db)

    response = client.get(
        f"/build/items/{SHIP_TYPE_ID}", params={"qty": 2, "build": str(COMPONENT_TYPE_ID)}
    )

    assert response.status_code == 200
    assert '<form method="get" class="qty-form">' in response.text
    assert '<input type="number" id="qty" name="qty" value="2" min="1">' in response.text
    assert f'<input type="hidden" name="build" value="{COMPONENT_TYPE_ID}">' in response.text


async def test_item_build_chain_scales_by_quantity(
    client: TestClient, mongo_db: AsyncMongoMockClient
) -> None:
    await _seed_buildable_ship(mongo_db)

    response = client.get(f"/build/items/{SHIP_TYPE_ID}", params={"qty": 3})

    assert response.status_code == 200
    assert '<input type="number" id="qty" name="qty" value="3" min="1">' in response.text
    assert '<span class="item-value">300</span>' in response.text


async def test_item_build_chain_404s_for_unknown_item(client: TestClient) -> None:
    response = client.get("/build/items/999999999")

    assert response.status_code == 404


async def test_item_build_chain_shows_only_first_level_materials_by_default(
    client: TestClient, mongo_db: AsyncMongoMockClient
) -> None:
    await _seed_two_level_ship(mongo_db)

    response = client.get(f"/build/items/{SHIP_TYPE_ID}")

    assert response.status_code == 200
    assert "Test Component" in response.text
    assert "Tritanium" not in response.text
    assert (
        f'href="/build/items/{SHIP_TYPE_ID}?qty=1&amp;build={COMPONENT_TYPE_ID}"' in response.text
    )
    assert 'class="flag flag-build"' in response.text
    assert 'class="item-card item-card-buildable"' in response.text


async def test_item_build_chain_expands_toggled_material(
    client: TestClient, mongo_db: AsyncMongoMockClient
) -> None:
    await _seed_two_level_ship(mongo_db)

    response = client.get(f"/build/items/{SHIP_TYPE_ID}", params={"build": str(COMPONENT_TYPE_ID)})

    assert response.status_code == 200
    assert "Tritanium" in response.text
    assert f'href="/build/items/{SHIP_TYPE_ID}?qty=1"' in response.text  # Buy toggle (collapse)
    assert '<a class="flag flag-buy-toggle" href=' in response.text


async def test_item_build_chain_shows_bought_flag_for_non_buildable_material(
    client: TestClient, mongo_db: AsyncMongoMockClient
) -> None:
    await _seed_buildable_ship(mongo_db)

    response = client.get(f"/build/items/{SHIP_TYPE_ID}")

    assert response.status_code == 200
    assert '<span class="flag flag-buy">Bought</span>' in response.text
    assert "item-card-buildable" not in response.text


async def test_item_build_chain_shows_not_buildable_message(
    client: TestClient, mongo_db: AsyncMongoMockClient
) -> None:
    await mongo_db.sde_types.insert_one(
        {"_id": TRITANIUM_TYPE_ID, "name": "Tritanium", "published": True}
    )

    response = client.get(f"/build/items/{TRITANIUM_TYPE_ID}")

    assert response.status_code == 200
    assert "can only" in response.text
    assert "be bought, not built" in response.text
