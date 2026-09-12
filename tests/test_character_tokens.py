from datetime import UTC, datetime, timedelta

import respx
from httpx import Response
from mongomock_motor import AsyncMongoMockClient

from app.core.config import Settings
from app.models.character import CharacterDocument
from app.services.character_tokens import ensure_fresh_tokens

PAST = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
FUTURE = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)


def _mongo_db() -> object:
    return AsyncMongoMockClient()["eve-build"]


def _character(**overrides: object) -> CharacterDocument:
    defaults: dict[str, object] = {
        "character_id": 1,
        "character_name": "Alice",
        "owner_hash": "hash",
        "scopes": [],
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "access_token_expires_at": FUTURE,
        "created_at": FUTURE,
        "updated_at": FUTURE,
    }
    defaults.update(overrides)
    return CharacterDocument.model_validate(defaults)


async def test_ensure_fresh_tokens_is_a_noop_when_access_token_still_valid() -> None:
    db = _mongo_db()
    character = _character()

    result = await ensure_fresh_tokens(db, Settings(), character)

    assert result is not None
    assert result.access_token == "old-access"


@respx.mock
async def test_ensure_fresh_tokens_refreshes_expired_access_token() -> None:
    settings = Settings()
    db = _mongo_db()
    character = _character(access_token_expires_at=PAST)
    await db.characters.insert_one({"_id": character.character_id, **character.model_dump()})

    respx.post(settings.eve_sso_token_url).mock(
        return_value=Response(
            200,
            json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 1200},
        )
    )

    result = await ensure_fresh_tokens(db, settings, character)

    assert result is not None
    assert result.access_token == "new-access"
    assert result.refresh_token == "new-refresh"

    doc = await db.characters.find_one({"_id": character.character_id})
    assert doc is not None
    assert doc["access_token"] == "new-access"


@respx.mock
async def test_ensure_fresh_tokens_returns_none_when_refresh_token_revoked() -> None:
    settings = Settings()
    db = _mongo_db()
    character = _character(access_token_expires_at=PAST)

    respx.post(settings.eve_sso_token_url).mock(return_value=Response(400))

    result = await ensure_fresh_tokens(db, settings, character)

    assert result is None


@respx.mock
async def test_ensure_fresh_tokens_disconnects_corp_data_when_corp_refresh_token_revoked() -> None:
    settings = Settings()
    db = _mongo_db()
    character = _character(
        corporation_id=99,
        corp_scopes=["esi-assets.read_corporation_assets.v1"],
        corp_access_token="old-corp-access",
        corp_refresh_token="old-corp-refresh",
        corp_access_token_expires_at=PAST,
    )
    await db.characters.insert_one({"_id": character.character_id, **character.model_dump()})

    respx.post(settings.eve_sso_token_url).mock(return_value=Response(401))

    result = await ensure_fresh_tokens(db, settings, character)

    assert result is not None
    assert result.corp_refresh_token is None
    assert result.corp_access_token is None
    assert result.corp_access_token_expires_at is None
    assert result.corp_scopes is None

    doc = await db.characters.find_one({"_id": character.character_id})
    assert doc is not None
    assert doc["corp_refresh_token"] is None
