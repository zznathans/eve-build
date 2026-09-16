from datetime import datetime

from pydantic import BaseModel


class CharacterDocument(BaseModel):
    character_id: int
    character_name: str
    owner_hash: str
    scopes: list[str]
    access_token: str
    refresh_token: str
    access_token_expires_at: datetime
    created_at: datetime
    updated_at: datetime

    # Set once a character opts into "Connect corporation data" (see
    # app/routes/auth.py connect_corp/disconnect_corp) - a separate incremental
    # OAuth grant from the base login above. corp_refresh_token being non-None
    # *is* the connected/opted-in flag; disconnecting nulls all five fields back out.
    corporation_id: int | None = None
    corp_scopes: list[str] | None = None
    corp_access_token: str | None = None
    corp_refresh_token: str | None = None
    corp_access_token_expires_at: datetime | None = None

    # The character's actual corporation/alliance membership, refreshed on every login -
    # independent of the "connect corporation data" opt-in above. Used for whitelist
    # enforcement (app/services/access_control.py) so per-request checks don't need an
    # ESI call.
    home_corporation_id: int | None = None
    home_alliance_id: int | None = None
