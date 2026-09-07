from urllib.parse import parse_qs, urlparse

import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from httpx import Response
from mongomock_motor import AsyncMongoMockClient

from app.core.config import Settings
from app.routes.blueprints import _resolve_container_chain
from app.services.esi import AssetEntry
from tests.conftest import make_access_token

CHARACTER_ID = 555
BLUEPRINT_ITEM_ID = 1001
BLUEPRINT_TYPE_ID = 588
TRITANIUM_TYPE_ID = 34
STATION_ID = 60003760
REACTION_ITEM_ID = 1004
REACTION_TYPE_ID = 989
REACTION_PRODUCT_TYPE_ID = 990


def _log_in(
    client: TestClient,
    test_settings: Settings,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
    scopes: list[str] | None = None,
) -> None:
    private_key, jwk = rsa_key_pair
    login_response = client.get("/auth/login", follow_redirects=False)
    state = parse_qs(urlparse(login_response.headers["location"]).query)["state"][0]

    access_token = make_access_token(
        private_key, character_id=CHARACTER_ID, character_name="Alt Pilot", scopes=scopes
    )
    respx.post(test_settings.eve_sso_token_url).mock(
        return_value=Response(
            200,
            json={
                "access_token": access_token,
                "refresh_token": "refresh-token-value",
                "expires_in": 1200,
            },
        )
    )
    respx.get(test_settings.eve_sso_jwks_url).mock(return_value=Response(200, json={"keys": [jwk]}))

    client.get(
        "/auth/callback",
        params={"code": "auth-code", "state": state},
        follow_redirects=False,
    )


def _mock_blueprints(settings: Settings) -> None:
    respx.get(
        f"{settings.esi_base_url}/characters/{CHARACTER_ID}/blueprints", params={"page": 1}
    ).mock(
        return_value=Response(
            200,
            headers={"X-Pages": "1"},
            json=[
                {
                    "item_id": BLUEPRINT_ITEM_ID,
                    "type_id": BLUEPRINT_TYPE_ID,
                    "location_id": STATION_ID,
                    "location_flag": "Hangar",
                    "quantity": -1,
                    "runs": -1,
                    "material_efficiency": 0,
                    "time_efficiency": 0,
                }
            ],
        )
    )


COPY_ITEM_ID = 1002
COPY_TYPE_ID = 589
T2_ITEM_ID = 1003
T2_TYPE_ID = 590


def _mock_blueprints_mixed(settings: Settings) -> None:
    respx.get(
        f"{settings.esi_base_url}/characters/{CHARACTER_ID}/blueprints", params={"page": 1}
    ).mock(
        return_value=Response(
            200,
            headers={"X-Pages": "1"},
            json=[
                {
                    "item_id": BLUEPRINT_ITEM_ID,
                    "type_id": BLUEPRINT_TYPE_ID,
                    "location_id": STATION_ID,
                    "location_flag": "Hangar",
                    "quantity": -1,
                    "runs": -1,
                    "material_efficiency": 0,
                    "time_efficiency": 0,
                },
                {
                    "item_id": COPY_ITEM_ID,
                    "type_id": COPY_TYPE_ID,
                    "location_id": STATION_ID,
                    "location_flag": "Hangar",
                    "quantity": -2,
                    "runs": 3,
                    "material_efficiency": 0,
                    "time_efficiency": 0,
                },
                {
                    "item_id": T2_ITEM_ID,
                    "type_id": T2_TYPE_ID,
                    "location_id": STATION_ID,
                    "location_flag": "Hangar",
                    "quantity": -1,
                    "runs": -1,
                    "material_efficiency": 10,
                    "time_efficiency": 20,
                },
            ],
        )
    )


async def _seed_mixed_sde(mongo_db: AsyncMongoMockClient) -> None:
    await mongo_db.sde_types.insert_many(
        [
            {
                "_id": BLUEPRINT_TYPE_ID,
                "name": "Rifter Blueprint",
                "group_id": 1,
                "published": True,
                "tech_level": 1,
            },
            {
                "_id": COPY_TYPE_ID,
                "name": "Merlin Blueprint",
                "group_id": 1,
                "published": True,
                "tech_level": 1,
            },
            {
                "_id": T2_TYPE_ID,
                "name": "Crow Blueprint",
                "group_id": 1,
                "published": True,
                "tech_level": 2,
            },
        ]
    )


def _mock_station_name(
    settings: Settings, station_id: int = STATION_ID, name: str = "Jita IV - Moon 4"
) -> None:
    respx.get(f"{settings.esi_base_url}/universe/stations/{station_id}").mock(
        return_value=Response(200, json={"name": name})
    )


def _mock_assets(settings: Settings, *, on_site: int, elsewhere: int) -> None:
    respx.get(f"{settings.esi_base_url}/characters/{CHARACTER_ID}/assets", params={"page": 1}).mock(
        return_value=Response(
            200,
            headers={"X-Pages": "1"},
            json=[
                {
                    "item_id": 1,
                    "type_id": TRITANIUM_TYPE_ID,
                    "location_id": STATION_ID,
                    "location_flag": "Hangar",
                    "location_type": "station",
                    "quantity": on_site,
                    "is_singleton": False,
                },
                {
                    "item_id": 2,
                    "type_id": TRITANIUM_TYPE_ID,
                    "location_id": 60003761,
                    "location_flag": "Hangar",
                    "location_type": "station",
                    "quantity": elsewhere,
                    "is_singleton": False,
                },
            ],
        )
    )


async def _seed_sde(mongo_db: AsyncMongoMockClient) -> None:
    await mongo_db.sde_types.insert_many(
        [
            {
                "_id": BLUEPRINT_TYPE_ID,
                "name": "Rifter Blueprint",
                "group_id": 1,
                "published": True,
            },
            {"_id": TRITANIUM_TYPE_ID, "name": "Tritanium", "group_id": 18, "published": True},
        ]
    )
    await mongo_db.sde_blueprints.insert_one(
        {
            "_id": BLUEPRINT_TYPE_ID,
            "product_type_id": 587,
            "product_quantity": 1,
            "manufacturing_time_seconds": 1200,
            "materials": [{"type_id": TRITANIUM_TYPE_ID, "quantity": 10}],
        }
    )


def _mock_reaction_formula(settings: Settings) -> None:
    respx.get(
        f"{settings.esi_base_url}/characters/{CHARACTER_ID}/blueprints", params={"page": 1}
    ).mock(
        return_value=Response(
            200,
            headers={"X-Pages": "1"},
            json=[
                {
                    "item_id": REACTION_ITEM_ID,
                    "type_id": REACTION_TYPE_ID,
                    "location_id": STATION_ID,
                    "location_flag": "Hangar",
                    "quantity": -1,
                    "runs": -1,
                    "material_efficiency": 0,
                    "time_efficiency": 0,
                }
            ],
        )
    )


async def _seed_reaction_sde(mongo_db: AsyncMongoMockClient) -> None:
    await mongo_db.sde_types.insert_many(
        [
            {
                "_id": REACTION_TYPE_ID,
                "name": "Methanofullerene Reaction Formula",
                "group_id": 1,
                "published": True,
                "tech_level": None,
            },
            {"_id": TRITANIUM_TYPE_ID, "name": "Tritanium", "group_id": 18, "published": True},
        ]
    )
    await mongo_db.sde_blueprints.insert_one(
        {
            "_id": REACTION_TYPE_ID,
            "product_type_id": REACTION_PRODUCT_TYPE_ID,
            "product_quantity": 100,
            "manufacturing_time_seconds": 1800,
            "materials": [{"type_id": TRITANIUM_TYPE_ID, "quantity": 100}],
            "activity_id": 11,
        }
    )


@respx.mock
async def test_list_blueprints_shows_owned_reaction_formula(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    await _seed_reaction_sde(mongo_db)
    _mock_reaction_formula(test_settings)
    _mock_assets(test_settings, on_site=0, elsewhere=0)
    _mock_station_name(test_settings)

    response = client.get("/blueprints")

    assert response.status_code == 200
    assert "Methanofullerene Reaction Formula" in response.text


@respx.mock
async def test_blueprint_detail_computes_buildable_counts_for_reaction_formula(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    await _seed_reaction_sde(mongo_db)
    _mock_reaction_formula(test_settings)
    _mock_assets(test_settings, on_site=350, elsewhere=0)
    _mock_station_name(test_settings)

    response = client.get(f"/blueprints/{REACTION_ITEM_ID}")

    assert response.status_code == 200
    assert "Methanofullerene Reaction Formula" in response.text
    # needs 100 Tritanium/run, has 350 on-site -> 3 buildable
    assert ">3<" in response.text
    assert "Tritanium" in response.text


@respx.mock
async def test_list_blueprints_shows_owned_blueprint(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    await _seed_sde(mongo_db)
    _mock_blueprints(test_settings)
    _mock_assets(test_settings, on_site=6, elsewhere=20)
    _mock_station_name(test_settings)

    response = client.get("/blueprints")

    assert response.status_code == 200
    assert "Rifter Blueprint" in response.text
    assert f'href="/blueprints/{BLUEPRINT_ITEM_ID}"' in response.text
    assert "0/10" in response.text
    assert "0/20" in response.text
    assert "Jita IV - Moon 4" in response.text
    assert ">Location<" in response.text


@respx.mock
async def test_list_blueprints_shows_location_security_status(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    system_id = 30000142
    _log_in(client, test_settings, rsa_key_pair)
    await _seed_sde(mongo_db)
    _mock_blueprints(test_settings)
    _mock_assets(test_settings, on_site=6, elsewhere=20)
    respx.get(f"{test_settings.esi_base_url}/universe/stations/{STATION_ID}").mock(
        return_value=Response(200, json={"name": "Jita IV - Moon 4", "system_id": system_id})
    )
    respx.get(f"{test_settings.esi_base_url}/universe/systems/{system_id}").mock(
        return_value=Response(200, json={"security_status": 0.9459991455078125})
    )

    response = client.get("/blueprints")

    assert response.status_code == 200
    assert 'Jita IV - Moon 4 (<span style="color: #48f0c0;">0.9</span>)' in response.text


def _asset(item_id: int, location_id: int, location_type: str) -> AssetEntry:
    return AssetEntry(
        item_id=item_id,
        type_id=0,
        location_id=location_id,
        location_flag="Unlocked",
        location_type=location_type,
        quantity=1,
        is_singleton=False,
    )


def test_resolve_container_chain_returns_id_unchanged_when_not_a_container() -> None:
    assert _resolve_container_chain(STATION_ID, {}) == STATION_ID


def test_resolve_container_chain_walks_nested_containers_to_a_station() -> None:
    # blueprint sits in container A, which sits in container B, which is docked at a station -
    # mirrors the real shape ESI returns for a blueprint stashed inside a secure container.
    container_a_id = 1048708888563
    container_b_id = 1044850845132
    assets_by_item_id = {
        container_a_id: _asset(container_a_id, container_b_id, "item"),
        container_b_id: _asset(container_b_id, STATION_ID, "station"),
    }

    assert _resolve_container_chain(container_a_id, assets_by_item_id) == STATION_ID


def test_resolve_container_chain_does_not_infinite_loop_on_a_cycle() -> None:
    # shouldn't happen in real data, but guard against ESI returning a malformed/circular chain
    assets_by_item_id = {
        1: _asset(1, 2, "item"),
        2: _asset(2, 1, "item"),
    }

    assert _resolve_container_chain(1, assets_by_item_id) in (1, 2)


@respx.mock
async def test_blueprint_detail_resolves_location_through_nested_containers(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    container_a_id = 1048708888563
    container_b_id = 1044850845132

    _log_in(client, test_settings, rsa_key_pair)
    await _seed_sde(mongo_db)
    respx.get(
        f"{test_settings.esi_base_url}/characters/{CHARACTER_ID}/blueprints", params={"page": 1}
    ).mock(
        return_value=Response(
            200,
            headers={"X-Pages": "1"},
            json=[
                {
                    "item_id": BLUEPRINT_ITEM_ID,
                    "type_id": BLUEPRINT_TYPE_ID,
                    "location_id": container_a_id,
                    "location_flag": "Unlocked",
                    "quantity": -1,
                    "runs": -1,
                    "material_efficiency": 0,
                    "time_efficiency": 0,
                }
            ],
        )
    )
    respx.get(
        f"{test_settings.esi_base_url}/characters/{CHARACTER_ID}/assets", params={"page": 1}
    ).mock(
        return_value=Response(
            200,
            headers={"X-Pages": "1"},
            json=[
                {
                    "item_id": container_a_id,
                    "type_id": 1067,
                    "location_id": container_b_id,
                    "location_flag": "Unlocked",
                    "location_type": "item",
                    "quantity": 1,
                    "is_singleton": False,
                },
                {
                    "item_id": container_b_id,
                    "type_id": 17366,
                    "location_id": STATION_ID,
                    "location_flag": "Hangar",
                    "location_type": "station",
                    "quantity": 1,
                    "is_singleton": True,
                },
            ],
        )
    )
    respx.get(f"{test_settings.esi_base_url}/universe/stations/{STATION_ID}").mock(
        return_value=Response(200, json={"name": "Jita IV - Moon 4"})
    )

    response = client.get(f"/blueprints/{BLUEPRINT_ITEM_ID}")

    assert response.status_code == 200
    assert "Jita IV - Moon 4" in response.text
    assert f"Location {container_a_id}" not in response.text


@respx.mock
async def test_blueprint_detail_computes_buildable_counts(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    await _seed_sde(mongo_db)
    _mock_blueprints(test_settings)
    _mock_assets(test_settings, on_site=6, elsewhere=20)
    respx.get(f"{test_settings.esi_base_url}/universe/stations/{STATION_ID}").mock(
        return_value=Response(200, json={"name": "Jita IV - Moon 4"})
    )

    response = client.get(f"/blueprints/{BLUEPRINT_ITEM_ID}")

    assert response.status_code == 200
    assert "Rifter Blueprint" in response.text
    assert "Jita IV - Moon 4" in response.text
    # needs 10 Tritanium/run, has 6 on-site (0 buildable) + 20 elsewhere (26 total -> 2 buildable)
    assert ">0<" in response.text
    assert ">2<" in response.text
    assert "Tritanium" in response.text


@respx.mock
async def test_blueprint_detail_shows_add_to_plan_button(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    await _seed_sde(mongo_db)
    _mock_blueprints(test_settings)
    _mock_assets(test_settings, on_site=6, elsewhere=20)
    respx.get(f"{test_settings.esi_base_url}/universe/stations/{STATION_ID}").mock(
        return_value=Response(200, json={"name": "Jita IV - Moon 4"})
    )

    response = client.get(f"/blueprints/{BLUEPRINT_ITEM_ID}")

    assert response.status_code == 200
    assert "Add to Plan" in response.text
    assert 'href="/plans/create?type_id=587&amp;qty=1"' in response.text


@respx.mock
async def test_blueprint_detail_shows_price_info_per_run(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    await _seed_sde(mongo_db)
    _mock_blueprints(test_settings)
    _mock_assets(test_settings, on_site=6, elsewhere=20)
    _mock_station_name(test_settings)
    await mongo_db.market_prices.insert_many(
        [
            {"_id": TRITANIUM_TYPE_ID, "adjusted_price": 5.0, "average_price": 5.0},
            {"_id": 587, "adjusted_price": 100.0, "average_price": 100.0},
        ]
    )

    response = client.get(f"/blueprints/{BLUEPRINT_ITEM_ID}")

    assert response.status_code == 200
    # 0 ME needs 10 Tritanium/run * 5 ISK = 50 ISK cost; product (type 587) is worth
    # 1 * 100 ISK = 100 ISK; profit is the 50 ISK difference. These render as
    # summary-stat tiles (value/label pairs), not as one inline string.
    assert "Cost / run" in response.text
    assert "Output / run" in response.text
    assert "Profit / run" in response.text
    assert '<div class="value">50 ISK</div>' in response.text
    assert '<div class="value">100 ISK</div>' in response.text


SHIP_BP_ITEM_ID = 2001
SHIP_BP_TYPE_ID = 6100
SHIP_PRODUCT_TYPE_ID = 6101
MODULE_BP_ITEM_ID = 2002
MODULE_BP_TYPE_ID = 6200
MODULE_PRODUCT_TYPE_ID = 6201


@respx.mock
async def test_list_blueprints_tiles_all_cards_without_grouping(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    await mongo_db.sde_types.insert_many(
        [
            {"_id": SHIP_BP_TYPE_ID, "name": "Widget Blueprint", "group_id": 1, "published": True},
            {
                "_id": MODULE_BP_TYPE_ID,
                "name": "Gadget Blueprint",
                "group_id": 1,
                "published": True,
            },
            {"_id": SHIP_PRODUCT_TYPE_ID, "name": "Widget", "group_id": 2, "published": True},
            {
                "_id": MODULE_PRODUCT_TYPE_ID,
                "name": "Gadget",
                "group_id": 3,
                "published": True,
            },
        ]
    )
    await mongo_db.sde_blueprints.insert_many(
        [
            {
                "_id": SHIP_BP_TYPE_ID,
                "product_type_id": SHIP_PRODUCT_TYPE_ID,
                "product_quantity": 1,
                "manufacturing_time_seconds": 600,
                "materials": [],
            },
            {
                "_id": MODULE_BP_TYPE_ID,
                "product_type_id": MODULE_PRODUCT_TYPE_ID,
                "product_quantity": 1,
                "manufacturing_time_seconds": 600,
                "materials": [],
            },
        ]
    )
    respx.get(
        f"{test_settings.esi_base_url}/characters/{CHARACTER_ID}/blueprints", params={"page": 1}
    ).mock(
        return_value=Response(
            200,
            headers={"X-Pages": "1"},
            json=[
                {
                    "item_id": SHIP_BP_ITEM_ID,
                    "type_id": SHIP_BP_TYPE_ID,
                    "location_id": STATION_ID,
                    "location_flag": "Hangar",
                    "quantity": -1,
                    "runs": -1,
                    "material_efficiency": 0,
                    "time_efficiency": 0,
                },
                {
                    "item_id": MODULE_BP_ITEM_ID,
                    "type_id": MODULE_BP_TYPE_ID,
                    "location_id": STATION_ID,
                    "location_flag": "Hangar",
                    "quantity": -1,
                    "runs": -1,
                    "material_efficiency": 0,
                    "time_efficiency": 0,
                },
            ],
        )
    )
    _mock_assets(test_settings, on_site=0, elsewhere=0)
    _mock_station_name(test_settings)

    response = client.get("/blueprints")

    assert response.status_code == 200
    assert "Widget Blueprint" in response.text
    assert "Gadget Blueprint" in response.text
    # No more per-category <h2> grouping - a single item-grid holds every card.
    assert "<h2>" not in response.text
    assert response.text.count('class="item-grid"') == 1
    # Card backgrounds use the *product's* icon, not the blueprint's own icon.
    assert f"https://images.evetech.net/types/{SHIP_PRODUCT_TYPE_ID}/icon" in response.text
    assert f"https://images.evetech.net/types/{MODULE_PRODUCT_TYPE_ID}/icon" in response.text


@respx.mock
async def test_blueprint_detail_retries_location_after_failed_lookup(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    await _seed_sde(mongo_db)
    _mock_blueprints(test_settings)
    _mock_assets(test_settings, on_site=6, elsewhere=20)
    station_route = respx.get(f"{test_settings.esi_base_url}/universe/stations/{STATION_ID}")
    station_route.mock(
        side_effect=[
            Response(403, json={"error": "no access"}),
            Response(200, json={"name": "Jita IV - Moon 4"}),
        ]
    )

    first_response = client.get(f"/blueprints/{BLUEPRINT_ITEM_ID}")
    assert first_response.status_code == 200
    assert f"Location {STATION_ID}" in first_response.text

    # A failed lookup must not be cached, so it's retried rather than stuck as "unresolved".
    assert await mongo_db.location_names.find_one({"_id": STATION_ID}) is None

    second_response = client.get(f"/blueprints/{BLUEPRINT_ITEM_ID}")
    assert second_response.status_code == 200
    assert "Jita IV - Moon 4" in second_response.text
    assert station_route.call_count == 2


@respx.mock
async def test_list_blueprints_default_filter_shows_only_t1_originals(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    await _seed_mixed_sde(mongo_db)
    _mock_blueprints_mixed(test_settings)
    _mock_assets(test_settings, on_site=0, elsewhere=0)
    _mock_station_name(test_settings)

    response = client.get("/blueprints")

    assert response.status_code == 200
    assert "Rifter Blueprint" in response.text
    assert "Merlin Blueprint" not in response.text
    assert "Crow Blueprint" not in response.text


@respx.mock
async def test_list_blueprints_filters_can_reveal_copies_and_t2(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    await _seed_mixed_sde(mongo_db)
    _mock_blueprints_mixed(test_settings)
    _mock_assets(test_settings, on_site=0, elsewhere=0)
    _mock_station_name(test_settings)

    response = client.get(
        "/blueprints",
        params=[("f", "1"), ("show", "original"), ("show", "copy"), ("show", "t2")],
    )

    assert response.status_code == 200
    assert "Rifter Blueprint" in response.text
    assert "Merlin Blueprint" in response.text
    assert "Crow Blueprint" in response.text


@respx.mock
async def test_list_blueprints_search_filters_by_name_case_insensitively(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    await _seed_mixed_sde(mongo_db)
    _mock_blueprints_mixed(test_settings)
    _mock_assets(test_settings, on_site=0, elsewhere=0)
    _mock_station_name(test_settings)

    response = client.get(
        "/blueprints",
        params=[
            ("f", "1"),
            ("show", "original"),
            ("show", "copy"),
            ("show", "t2"),
            ("search", "RIFTER"),
        ],
    )

    assert response.status_code == 200
    assert "Rifter Blueprint" in response.text
    assert "Merlin Blueprint" not in response.text
    assert "Crow Blueprint" not in response.text


@respx.mock
async def test_list_blueprints_search_box_preserves_typed_value(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    await _seed_sde(mongo_db)
    _mock_blueprints(test_settings)
    _mock_assets(test_settings, on_site=0, elsewhere=0)
    _mock_station_name(test_settings)

    response = client.get("/blueprints", params={"search": "rift"})

    assert response.status_code == 200
    assert 'name="search" value="rift"' in response.text


@respx.mock
async def test_list_blueprints_sort_by_me_descending(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    await _seed_mixed_sde(mongo_db)
    _mock_blueprints_mixed(test_settings)
    _mock_assets(test_settings, on_site=0, elsewhere=0)
    _mock_station_name(test_settings)

    response = client.get(
        "/blueprints",
        params=[
            ("f", "1"),
            ("show", "original"),
            ("show", "copy"),
            ("show", "t2"),
            ("sort", "me"),
            ("dir", "desc"),
        ],
    )

    assert response.status_code == 200
    crow_index = response.text.index("Crow Blueprint")
    rifter_index = response.text.index("Rifter Blueprint")
    assert crow_index < rifter_index


@respx.mock
async def test_list_blueprints_location_dropdown_filters_by_location(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    second_station_id = 60003762
    second_blueprint_item_id = 1004

    _log_in(client, test_settings, rsa_key_pair)
    await _seed_sde(mongo_db)
    await mongo_db.sde_types.insert_one(
        {"_id": COPY_TYPE_ID, "name": "Merlin Blueprint", "group_id": 1, "published": True}
    )
    respx.get(
        f"{test_settings.esi_base_url}/characters/{CHARACTER_ID}/blueprints", params={"page": 1}
    ).mock(
        return_value=Response(
            200,
            headers={"X-Pages": "1"},
            json=[
                {
                    "item_id": BLUEPRINT_ITEM_ID,
                    "type_id": BLUEPRINT_TYPE_ID,
                    "location_id": STATION_ID,
                    "location_flag": "Hangar",
                    "quantity": -1,
                    "runs": -1,
                    "material_efficiency": 0,
                    "time_efficiency": 0,
                },
                {
                    "item_id": second_blueprint_item_id,
                    "type_id": COPY_TYPE_ID,
                    "location_id": second_station_id,
                    "location_flag": "Hangar",
                    "quantity": -1,
                    "runs": -1,
                    "material_efficiency": 0,
                    "time_efficiency": 0,
                },
            ],
        )
    )
    _mock_assets(test_settings, on_site=0, elsewhere=0)
    _mock_station_name(test_settings, STATION_ID, "Jita IV - Moon 4")
    _mock_station_name(test_settings, second_station_id, "Amarr VIII - Emperor Family Academy")

    all_response = client.get("/blueprints")
    assert "Rifter Blueprint" in all_response.text
    assert "Merlin Blueprint" in all_response.text
    assert '<option value="">All locations</option>' in all_response.text
    assert f'<option value="{STATION_ID}"' in all_response.text
    assert f'<option value="{second_station_id}"' in all_response.text

    filtered_response = client.get("/blueprints", params={"location": str(second_station_id)})
    assert filtered_response.status_code == 200
    assert "Merlin Blueprint" in filtered_response.text
    assert "Rifter Blueprint" not in filtered_response.text
    assert f'value="{second_station_id}" selected' in filtered_response.text


@respx.mock
async def test_blueprint_detail_404_for_unknown_item(
    client: TestClient,
    test_settings: Settings,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    _mock_blueprints(test_settings)

    response = client.get("/blueprints/999999")

    assert response.status_code == 404


@respx.mock
async def test_list_blueprints_serves_names_from_redis_cache(
    client_with_redis: TestClient,
    fake_redis: object,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client_with_redis, test_settings, rsa_key_pair)
    await _seed_sde(mongo_db)
    _mock_blueprints(test_settings)
    _mock_assets(test_settings, on_site=0, elsewhere=0)
    _mock_station_name(test_settings)

    first_response = client_with_redis.get("/blueprints")
    assert first_response.status_code == 200
    assert "Rifter Blueprint" in first_response.text

    cached = await fake_redis.get(f"sde_type:{BLUEPRINT_TYPE_ID}")  # type: ignore[attr-defined]
    assert cached is not None

    # Delete the Mongo doc entirely - a second request must still resolve the
    # name from Redis rather than falling back to "Type {id}".
    await mongo_db.sde_types.delete_one({"_id": BLUEPRINT_TYPE_ID})

    second_response = client_with_redis.get("/blueprints")

    assert second_response.status_code == 200
    assert "Rifter Blueprint" in second_response.text
