from datetime import UTC, datetime, timedelta

import httpx
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import Settings
from app.models.character import CharacterDocument
from app.services import eve_sso


async def ensure_fresh_tokens(
    db: AsyncIOMotorDatabase, settings: Settings, document: CharacterDocument
) -> CharacterDocument | None:
    """Refreshes document's access_token (and corp_access_token, if connected) in place
    when expired, persisting the new tokens to Mongo. Returns None if the personal
    refresh_token itself has been revoked (400/401) - callers should treat that as "no
    longer authenticated" for this character. A revoked corp refresh token instead just
    clears the four corp_* fields (disconnects corp data) without affecting personal data,
    matching app/routes/auth.py's disconnect_corp behavior."""
    updates: dict[str, object] = {}

    if document.access_token_expires_at <= _utcnow_naive():
        try:
            token = await eve_sso.refresh_access_token(
                settings, refresh_token=document.refresh_token
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (400, 401):
                return None
            raise
        document.access_token = token.access_token
        document.refresh_token = token.refresh_token
        document.access_token_expires_at = _expires_at(token.expires_in)
        updates["access_token"] = document.access_token
        updates["refresh_token"] = document.refresh_token
        updates["access_token_expires_at"] = document.access_token_expires_at

    if (
        document.corp_refresh_token is not None
        and document.corp_access_token_expires_at is not None
        and document.corp_access_token_expires_at <= _utcnow_naive()
    ):
        try:
            corp_token = await eve_sso.refresh_access_token(
                settings, refresh_token=document.corp_refresh_token
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (400, 401):
                document.corp_scopes = None
                document.corp_access_token = None
                document.corp_refresh_token = None
                document.corp_access_token_expires_at = None
                updates["corp_scopes"] = None
                updates["corp_access_token"] = None
                updates["corp_refresh_token"] = None
                updates["corp_access_token_expires_at"] = None
            else:
                raise
        else:
            document.corp_access_token = corp_token.access_token
            document.corp_refresh_token = corp_token.refresh_token
            document.corp_access_token_expires_at = _expires_at(corp_token.expires_in)
            updates["corp_access_token"] = document.corp_access_token
            updates["corp_refresh_token"] = document.corp_refresh_token
            updates["corp_access_token_expires_at"] = document.corp_access_token_expires_at

    if updates:
        document.updated_at = _utcnow_naive()
        updates["updated_at"] = document.updated_at
        await db.characters.update_one({"_id": document.character_id}, {"$set": updates})

    return document


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _expires_at(expires_in: int) -> datetime:
    return _utcnow_naive() + timedelta(seconds=expires_in)
