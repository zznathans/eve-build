from app.core.config import Settings
from app.services.access_control import is_character_allowed


def _settings(
    *, character_ids: str = "", corporation_ids: str = "", alliance_ids: str = ""
) -> Settings:
    return Settings(
        access_whitelist_character_ids=character_ids,
        access_whitelist_corporation_ids=corporation_ids,
        access_whitelist_alliance_ids=alliance_ids,
    )


def test_allows_everyone_when_no_whitelist_configured() -> None:
    settings = _settings()

    assert is_character_allowed(settings, character_id=1, corporation_id=2, alliance_id=None)


def test_allows_character_id_match() -> None:
    settings = _settings(character_ids="111,222")

    assert is_character_allowed(settings, character_id=111, corporation_id=999, alliance_id=None)


def test_allows_corporation_id_match() -> None:
    settings = _settings(corporation_ids="555")

    assert is_character_allowed(settings, character_id=1, corporation_id=555, alliance_id=None)


def test_allows_alliance_id_match() -> None:
    settings = _settings(alliance_ids="777")

    assert is_character_allowed(settings, character_id=1, corporation_id=2, alliance_id=777)


def test_rejects_when_nothing_matches_a_configured_whitelist() -> None:
    settings = _settings(character_ids="111", corporation_ids="555", alliance_ids="777")

    assert not is_character_allowed(settings, character_id=1, corporation_id=2, alliance_id=3)


def test_rejects_none_alliance_id_against_alliance_whitelist() -> None:
    settings = _settings(alliance_ids="777")

    assert not is_character_allowed(settings, character_id=1, corporation_id=2, alliance_id=None)
