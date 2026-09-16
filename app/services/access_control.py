from app.core.config import Settings


def _parse_id_set(value: str) -> set[int]:
    return {int(part) for part in value.split(",") if part.strip()}


def is_character_allowed(
    settings: Settings, *, character_id: int, corporation_id: int, alliance_id: int | None
) -> bool:
    """Whether a character is permitted to use the app, per the access_whitelist_*
    settings. All three whitelists empty means no restriction - open access."""
    character_ids = _parse_id_set(settings.access_whitelist_character_ids)
    corporation_ids = _parse_id_set(settings.access_whitelist_corporation_ids)
    alliance_ids = _parse_id_set(settings.access_whitelist_alliance_ids)
    if not character_ids and not corporation_ids and not alliance_ids:
        return True

    if character_id in character_ids:
        return True
    if corporation_id in corporation_ids:
        return True
    return alliance_id is not None and alliance_id in alliance_ids
