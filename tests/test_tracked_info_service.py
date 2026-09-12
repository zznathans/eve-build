from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from fakeredis.aioredis import FakeRedis
from httpx import Response
from mongomock_motor import AsyncMongoMockClient

from app.core.config import Settings
from app.db import rabbitmq
from app.services import tracked_info

FAR_FUTURE = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1)


def _mongo_db() -> object:
    return AsyncMongoMockClient()["eve-build"]


def _character_doc(
    character_id: int,
    *,
    corporation_id: int | None = None,
    corp_refresh_token: str | None = None,
) -> dict[str, object]:
    return {
        "_id": character_id,
        "character_id": character_id,
        "character_name": f"Character {character_id}",
        "owner_hash": "hash",
        "scopes": [],
        "access_token": "token",
        "refresh_token": "refresh",
        "access_token_expires_at": FAR_FUTURE,
        "created_at": FAR_FUTURE,
        "updated_at": FAR_FUTURE,
        "corporation_id": corporation_id,
        "corp_scopes": None,
        "corp_access_token": "corp-token" if corp_refresh_token else None,
        "corp_refresh_token": corp_refresh_token,
        "corp_access_token_expires_at": FAR_FUTURE if corp_refresh_token else None,
    }


async def test_enqueue_all_refresh_jobs_publishes_one_job_per_personal_source() -> None:
    db = _mongo_db()
    await db.characters.insert_one(_character_doc(1))

    published: list[rabbitmq.TrackedInfoRefreshJobMessage] = []

    async def publish(queue_name: str, body: bytes) -> None:
        assert queue_name == rabbitmq.TRACKED_INFO_REFRESH_JOBS_QUEUE
        published.append(rabbitmq.decode_tracked_info_refresh_job(body))

    count = await tracked_info.enqueue_all_refresh_jobs(db, publish)

    assert count == 4
    assert {job.kind for job in published} == {
        "character_assets",
        "character_blueprints",
        "character_industry_jobs",
        "character_colonies",
    }
    assert all(job.character_id == 1 and job.corporation_id is None for job in published)


async def test_enqueue_all_refresh_jobs_dedupes_corp_jobs_by_corporation() -> None:
    db = _mongo_db()
    await db.characters.insert_one(
        _character_doc(1, corporation_id=99, corp_refresh_token="corp-refresh")
    )
    await db.characters.insert_one(
        _character_doc(2, corporation_id=99, corp_refresh_token="corp-refresh")
    )

    published: list[rabbitmq.TrackedInfoRefreshJobMessage] = []

    async def publish(queue_name: str, body: bytes) -> None:
        published.append(rabbitmq.decode_tracked_info_refresh_job(body))

    await tracked_info.enqueue_all_refresh_jobs(db, publish)

    corp_jobs = [job for job in published if job.corporation_id is not None]
    assert len(corp_jobs) == 3
    assert {job.kind for job in corp_jobs} == {
        "corporation_assets",
        "corporation_blueprints",
        "corporation_industry_jobs",
    }
    assert {job.character_id for job in corp_jobs} == {1}
    assert {job.corporation_id for job in corp_jobs} == {99}


@respx.mock
async def test_run_refresh_job_warms_the_cache_for_a_personal_source() -> None:
    settings = Settings()
    db = _mongo_db()
    redis = FakeRedis()
    character_id = 555
    await db.characters.insert_one(_character_doc(character_id))

    respx.get(f"{settings.esi_base_url}/characters/{character_id}/assets").mock(
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

    job = rabbitmq.TrackedInfoRefreshJobMessage(kind="character_assets", character_id=character_id)
    await tracked_info.run_refresh_job(db, redis, settings, job)

    docs = await db.assets.find({"character_id": character_id}).to_list(None)
    assert len(docs) == 1
    assert docs[0]["type_id"] == 34


async def test_run_refresh_job_skips_unknown_character() -> None:
    settings = Settings()
    db = _mongo_db()
    redis = FakeRedis()

    job = rabbitmq.TrackedInfoRefreshJobMessage(kind="character_assets", character_id=404)

    await tracked_info.run_refresh_job(db, redis, settings, job)


async def test_run_refresh_job_skips_when_refresh_token_revoked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings()
    db = _mongo_db()
    redis = FakeRedis()
    character_id = 555
    await db.characters.insert_one(_character_doc(character_id))

    async def _revoked(db: object, settings: object, document: object) -> None:
        return None

    monkeypatch.setattr(tracked_info, "ensure_fresh_tokens", _revoked)

    job = rabbitmq.TrackedInfoRefreshJobMessage(kind="character_assets", character_id=character_id)
    await tracked_info.run_refresh_job(db, redis, settings, job)


async def test_run_refresh_job_skips_corp_source_missing_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings()
    db = _mongo_db()
    redis = FakeRedis()
    character_id = 555
    await db.characters.insert_one(
        _character_doc(character_id, corporation_id=99, corp_refresh_token="corp-refresh")
    )

    async def _forbidden(*args: object, **kwargs: object) -> None:
        request = httpx.Request("GET", "https://esi.evetech.net/corporations/99/assets")
        response = httpx.Response(403, request=request)
        raise httpx.HTTPStatusError("forbidden", request=request, response=response)

    monkeypatch.setattr(tracked_info, "_CORP_HANDLERS", {"corporation_assets": _forbidden})

    job = rabbitmq.TrackedInfoRefreshJobMessage(
        kind="corporation_assets", character_id=character_id, corporation_id=99
    )
    await tracked_info.run_refresh_job(db, redis, settings, job)


async def test_run_refresh_job_skips_corp_source_without_delegate_token() -> None:
    settings = Settings()
    db = _mongo_db()
    redis = FakeRedis()
    character_id = 555
    await db.characters.insert_one(_character_doc(character_id))

    job = rabbitmq.TrackedInfoRefreshJobMessage(
        kind="corporation_assets", character_id=character_id, corporation_id=99
    )
    await tracked_info.run_refresh_job(db, redis, settings, job)
