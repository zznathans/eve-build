from datetime import UTC, datetime

import respx
from fakeredis.aioredis import FakeRedis
from httpx import Response
from mongomock_motor import AsyncMongoMockClient

from app.core.config import Settings
from app.models.character import CharacterDocument
from app.services import character_data

CHARACTER_ID = 555
CORPORATION_ID = 98000001


def _character(**overrides: object) -> CharacterDocument:
    now = datetime.now(UTC)
    defaults: dict[str, object] = {
        "character_id": CHARACTER_ID,
        "character_name": "Alt Pilot",
        "owner_hash": "hash",
        "scopes": [],
        "access_token": "token",
        "refresh_token": "refresh-token",
        "access_token_expires_at": now,
        "created_at": now,
        "updated_at": now,
    }
    defaults.update(overrides)
    return CharacterDocument(**defaults)


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


@respx.mock
async def test_get_merged_assets_includes_corp_assets_when_connected() -> None:
    settings = Settings()
    db = _mongo_db()
    redis = FakeRedis()
    character = _character(
        corporation_id=CORPORATION_ID,
        corp_scopes=["esi-assets.read_corporation_assets.v1"],
        corp_access_token="corp-token",
        corp_refresh_token="corp-refresh-token",
        corp_access_token_expires_at=datetime.now(UTC),
    )

    respx.get(
        f"{settings.esi_base_url}/characters/{CHARACTER_ID}/assets", params={"page": 1}
    ).mock(return_value=Response(200, headers={"X-Pages": "1"}, json=[]))
    respx.get(
        f"{settings.esi_base_url}/corporations/{CORPORATION_ID}/assets", params={"page": 1}
    ).mock(
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
                    "quantity": 250,
                    "is_singleton": False,
                }
            ],
        )
    )

    assets, corp_included = await character_data.get_merged_assets(db, redis, settings, character)

    assert corp_included is True
    assert [asset.quantity for asset in assets] == [250]


@respx.mock
async def test_get_merged_assets_excludes_corp_assets_when_not_connected() -> None:
    settings = Settings()
    db = _mongo_db()
    redis = FakeRedis()
    character = _character()
    _asset_response_route(settings)

    assets, corp_included = await character_data.get_merged_assets(db, redis, settings, character)

    assert corp_included is False
    assert len(assets) == 1
