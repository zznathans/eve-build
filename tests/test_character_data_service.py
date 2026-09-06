import respx
from fakeredis.aioredis import FakeRedis
from httpx import Response
from mongomock_motor import AsyncMongoMockClient

from app.core.config import Settings
from app.services import character_data

CHARACTER_ID = 555


def _mongo_db() -> object:
    return AsyncMongoMockClient()["eve-build"]


def _asset_response_route(settings: Settings) -> respx.Route:
    url = f"{settings.esi_base_url}/characters/{CHARACTER_ID}/assets"
    return respx.get(url, params={"page": 1}).mock(
        return_value=Response(
            200,
            headers={"X-Pages": "1"},
            json=[
                {
                    "item_id": 1,
                    "type_id": 34,
                    "location_id": 60003760,
                    "location_flag": "Hangar",
                    "location_type": "station",
                    "quantity": 100,
                    "is_singleton": False,
                }
            ],
        )
    )


@respx.mock
async def test_get_character_assets_persists_to_mongo() -> None:
    settings = Settings()
    db = _mongo_db()
    redis = FakeRedis()
    _asset_response_route(settings)

    assets = await character_data.get_character_assets(db, redis, settings, "token", CHARACTER_ID)

    assert len(assets) == 1
    assert assets[0].type_id == 34

    docs = await db.assets.find({"character_id": CHARACTER_ID}).to_list(None)
    assert len(docs) == 1
    assert docs[0]["type_id"] == 34
    assert docs[0]["character_id"] == CHARACTER_ID


@respx.mock
async def test_get_character_assets_second_call_uses_cache() -> None:
    settings = Settings()
    db = _mongo_db()
    redis = FakeRedis()
    route = _asset_response_route(settings)

    first = await character_data.get_character_assets(db, redis, settings, "token", CHARACTER_ID)
    second = await character_data.get_character_assets(db, redis, settings, "token", CHARACTER_ID)

    assert first == second
    assert route.call_count == 1


@respx.mock
async def test_get_character_colonies_skips_planet_that_fails_to_fetch() -> None:
    """One planet's detail fetch failing shouldn't blow up the whole character's colony
    refresh - the other planet(s) should still come back."""
    settings = Settings()
    db = _mongo_db()
    redis = FakeRedis()
    good_planet_id = 4001
    bad_planet_id = 4002

    respx.get(f"{settings.esi_base_url}/characters/{CHARACTER_ID}/planets/").mock(
        return_value=Response(
            200,
            json=[
                {
                    "planet_id": good_planet_id,
                    "solar_system_id": 30000142,
                    "planet_type": "gas",
                    "owner_id": CHARACTER_ID,
                    "last_update": "2026-01-01T00:00:00Z",
                    "upgrade_level": 3,
                    "num_pins": 1,
                },
                {
                    "planet_id": bad_planet_id,
                    "solar_system_id": 30000142,
                    "planet_type": "gas",
                    "owner_id": CHARACTER_ID,
                    "last_update": "2026-01-01T00:00:00Z",
                    "upgrade_level": 3,
                    "num_pins": 1,
                },
            ],
        )
    )
    respx.get(f"{settings.esi_base_url}/characters/{CHARACTER_ID}/planets/{good_planet_id}/").mock(
        return_value=Response(200, json={"pins": [], "links": [], "routes": []})
    )
    respx.get(f"{settings.esi_base_url}/characters/{CHARACTER_ID}/planets/{bad_planet_id}/").mock(
        return_value=Response(404, json={"error": "not found"})
    )

    colonies = await character_data.get_character_colonies(
        db, redis, settings, "token", CHARACTER_ID
    )

    assert [colony.planet_id for colony in colonies] == [good_planet_id]
