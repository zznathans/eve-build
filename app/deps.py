from fastapi import Depends, HTTPException, Request, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import Settings, get_settings
from app.db.mongo import get_database
from app.models.character import CharacterDocument
from app.services.character_tokens import ensure_fresh_tokens


async def get_current_character(
    request: Request,
    db: AsyncIOMotorDatabase = Depends(get_database),
    settings: Settings = Depends(get_settings),
) -> CharacterDocument:
    character = await get_current_character_optional(request, db, settings)
    if character is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    return character


async def get_current_character_optional(
    request: Request,
    db: AsyncIOMotorDatabase = Depends(get_database),
    settings: Settings = Depends(get_settings),
) -> CharacterDocument | None:
    character_id = request.session.get("character_id")
    if character_id is None:
        return None

    raw_doc = await db.characters.find_one({"_id": character_id})
    if raw_doc is None:
        return None

    document = CharacterDocument.model_validate(raw_doc)

    refreshed = await ensure_fresh_tokens(db, settings, document)
    if refreshed is None:
        request.session.pop("character_id", None)
        return None

    return refreshed
