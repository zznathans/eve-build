from datetime import UTC, datetime
from typing import cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from motor.motor_asyncio import AsyncIOMotorDatabase
from redis.asyncio import Redis

from app.core.config import Settings, get_settings
from app.db.mongo import get_database
from app.db.redis import get_redis
from app.deps import get_current_character
from app.models.character import CharacterDocument
from app.services import character_data, esi
from app.templating import templates

router = APIRouter(prefix="/settings", tags=["settings"])

_STYLE = ["/static/settings.css"]

_CORP_STATUS_SOURCES = (
    ("Assets", character_data.get_corporation_assets, "requires the Director role"),
    ("Blueprints", character_data.get_corporation_blueprints, "requires the Director role"),
    (
        "Industry jobs",
        character_data.get_corporation_industry_jobs,
        "requires the Director or Factory_Manager role",
    ),
)


def _format_timestamp(value: datetime) -> str:
    return value.replace(tzinfo=UTC).strftime("%Y-%m-%d %H:%M UTC")


@router.get("", response_class=HTMLResponse)
async def show_settings(
    request: Request,
    character: CharacterDocument = Depends(get_current_character),
    db: AsyncIOMotorDatabase = Depends(get_database),
    redis: Redis | None = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    account = {
        "avatar_url": f"https://images.evetech.net/characters/{character.character_id}"
        "/portrait?size=64",
        "created_at": _format_timestamp(character.created_at),
        "updated_at": _format_timestamp(character.updated_at),
        "scopes_text": ", ".join(sorted(character.scopes)) or "none",
    }

    sources = await character_data.data_summary(db, character)
    data_sources = [
        {
            "label": source.label,
            "shared": source.shared,
            "count": source.count,
            "last_updated": (
                _format_timestamp(source.cached_at) if source.cached_at else "never fetched yet"
            ),
        }
        for source in sources
    ]

    corp = None
    if character_data.corp_data_connected(character):
        corporation_id = cast(int, character.corporation_id)
        corp_access_token = cast(str, character.corp_access_token)
        corporation_name = await esi.get_corporation_name(settings, corporation_id)
        status_rows = []
        for label, fetch, required_role in _CORP_STATUS_SOURCES:
            result = await fetch(db, redis, settings, corp_access_token, corporation_id)
            status_rows.append(
                {
                    "label": label,
                    "ok": result is not None,
                    "count": len(result) if result is not None else 0,
                    "required_role": required_role,
                }
            )
        corp = {
            "label": corporation_name or f"Corporation {corporation_id}",
            "status_rows": status_rows,
        }

    return templates.TemplateResponse(
        request,
        "settings/index.html",
        {
            "character": character,
            "extra_stylesheets": _STYLE,
            "account": account,
            "data_sources": data_sources,
            "corp": corp,
        },
    )


@router.get("/refresh")
async def refresh_data(
    character: CharacterDocument = Depends(get_current_character),
    db: AsyncIOMotorDatabase = Depends(get_database),
    redis: Redis | None = Depends(get_redis),
) -> RedirectResponse:
    await character_data.refresh_character_data(db, redis, character)
    return RedirectResponse("/settings")


@router.get("/clear-data")
async def clear_data(
    request: Request,
    character: CharacterDocument = Depends(get_current_character),
    db: AsyncIOMotorDatabase = Depends(get_database),
    redis: Redis | None = Depends(get_redis),
) -> RedirectResponse:
    await character_data.clear_character_data(db, redis, character)
    request.session.clear()
    return RedirectResponse("/")
