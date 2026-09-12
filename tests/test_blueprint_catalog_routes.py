import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from app.core.config import Settings
from tests.test_blueprints_routes import _log_in

TRITANIUM_TYPE_ID = 34
RIFTER_TYPE_ID = 587
RIFTER_BLUEPRINT_TYPE_ID = 588
PUNISHER_TYPE_ID = 597
PUNISHER_BLUEPRINT_TYPE_ID = 598
FULLERENE_TYPE_ID = 990
REACTION_FORMULA_TYPE_ID = 989


async def _seed_catalog(mongo_db: AsyncMongoMockClient) -> None:
    await mongo_db.sde_types.insert_many(
        [
            {"_id": TRITANIUM_TYPE_ID, "name": "Tritanium", "published": True},
            {"_id": RIFTER_TYPE_ID, "name": "Rifter", "published": True},
            {
                "_id": RIFTER_BLUEPRINT_TYPE_ID,
                "name": "Rifter Blueprint",
                "published": True,
                "tech_level": None,
            },
            {"_id": PUNISHER_TYPE_ID, "name": "Punisher", "published": True},
            {
                "_id": PUNISHER_BLUEPRINT_TYPE_ID,
                "name": "Punisher Blueprint",
                "published": True,
                "tech_level": None,
            },
            {"_id": FULLERENE_TYPE_ID, "name": "Fullerene-C50", "published": True},
            {
                "_id": REACTION_FORMULA_TYPE_ID,
                "name": "Methanofullerene Reaction Formula",
                "published": True,
                "tech_level": None,
            },
        ]
    )
    await mongo_db.sde_blueprints.insert_many(
        [
            {
                "_id": RIFTER_BLUEPRINT_TYPE_ID,
                "product_type_id": RIFTER_TYPE_ID,
                "product_quantity": 1,
                "manufacturing_time_seconds": 1200,
                "materials": [{"type_id": TRITANIUM_TYPE_ID, "quantity": 100}],
                "activity_id": 1,
            },
            {
                "_id": PUNISHER_BLUEPRINT_TYPE_ID,
                "product_type_id": PUNISHER_TYPE_ID,
                "product_quantity": 1,
                "manufacturing_time_seconds": 1200,
                "materials": [{"type_id": TRITANIUM_TYPE_ID, "quantity": 80}],
                "activity_id": 1,
            },
            {
                "_id": REACTION_FORMULA_TYPE_ID,
                "product_type_id": FULLERENE_TYPE_ID,
                "product_quantity": 100,
                "manufacturing_time_seconds": 1800,
                "materials": [{"type_id": TRITANIUM_TYPE_ID, "quantity": 100}],
                "activity_id": 11,
            },
        ]
    )
    await mongo_db.market_prices.insert_many(
        [
            {"_id": TRITANIUM_TYPE_ID, "average_price": 5.0},
            {"_id": RIFTER_TYPE_ID, "average_price": 1000.0},
        ]
    )


@respx.mock
async def test_catalog_search_requires_two_characters(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    await _seed_catalog(mongo_db)

    response = client.get("/blueprints/catalog", params={"q": "r"})

    assert response.status_code == 200
    assert "at least 2 characters" in response.text


@respx.mock
async def test_catalog_search_matches_by_name_case_insensitive(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    await _seed_catalog(mongo_db)

    response = client.get("/blueprints/catalog", params={"q": "rIfTeR"})

    assert response.status_code == 200
    assert "Rifter Blueprint" in response.text
    assert "Punisher Blueprint" not in response.text
    assert f'href="/blueprints/catalog/{RIFTER_BLUEPRINT_TYPE_ID}"' in response.text


@respx.mock
async def test_catalog_search_no_matches(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    await _seed_catalog(mongo_db)

    response = client.get("/blueprints/catalog", params={"q": "zzznonexistent"})

    assert response.status_code == 200
    assert "No blueprints match" in response.text


@respx.mock
async def test_catalog_detail_shows_recipe_and_profit(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    await _seed_catalog(mongo_db)

    response = client.get(f"/blueprints/catalog/{RIFTER_BLUEPRINT_TYPE_ID}")

    assert response.status_code == 200
    assert "Rifter Blueprint" in response.text
    assert "Produces Rifter" in response.text
    assert "Tritanium" in response.text
    # Cost/run: 100 Tritanium * 5.0 ISK = 500 ISK. Output/run: 1 Rifter * 1000.0 ISK = 1000 ISK.
    assert "500 ISK" in response.text
    assert "1.0K ISK" in response.text


@respx.mock
async def test_catalog_search_matches_reaction_formulas(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    await _seed_catalog(mongo_db)

    response = client.get("/blueprints/catalog", params={"q": "methanofullerene"})

    assert response.status_code == 200
    assert "Methanofullerene Reaction Formula" in response.text
    assert f'href="/blueprints/catalog/{REACTION_FORMULA_TYPE_ID}"' in response.text


@respx.mock
async def test_catalog_detail_labels_reaction_formula(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    await _seed_catalog(mongo_db)

    response = client.get(f"/blueprints/catalog/{REACTION_FORMULA_TYPE_ID}")

    assert response.status_code == 200
    assert "Methanofullerene Reaction Formula" in response.text
    assert "Reaction formula" in response.text
    assert "Produces Fullerene-C50" in response.text
    assert "Tritanium" in response.text


@respx.mock
async def test_catalog_detail_404_for_unknown_blueprint(
    client: TestClient,
    test_settings: Settings,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)

    response = client.get("/blueprints/catalog/999999")

    assert response.status_code == 404


async def test_catalog_search_works_without_logging_in(
    client: TestClient,
    mongo_db: AsyncMongoMockClient,
) -> None:
    await _seed_catalog(mongo_db)

    response = client.get("/blueprints/catalog", params={"q": "rIfTeR"})

    assert response.status_code == 200
    assert "Rifter Blueprint" in response.text
    assert "Log in with EVE Online" in response.text
    assert 'href="/blueprints/catalog"' in response.text
    assert 'href="/planetary"' in response.text


async def test_catalog_detail_works_without_logging_in(
    client: TestClient,
    mongo_db: AsyncMongoMockClient,
) -> None:
    await _seed_catalog(mongo_db)

    response = client.get(f"/blueprints/catalog/{RIFTER_BLUEPRINT_TYPE_ID}")

    assert response.status_code == 200
    assert "Rifter Blueprint" in response.text
    assert "Log in with EVE Online" in response.text
