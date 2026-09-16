from datetime import datetime
from urllib.parse import parse_qs, urlparse

import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from httpx import Response
from mongomock_motor import AsyncMongoMockClient

from app.core.config import Settings
from tests.conftest import make_access_token

CHARACTER_ID = 555
CORPORATION_ID = 98000001
TRITANIUM_TYPE_ID = 34


def _log_in(
    client: TestClient,
    test_settings: Settings,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    private_key, jwk = rsa_key_pair
    login_response = client.get("/auth/login", follow_redirects=False)
    state = parse_qs(urlparse(login_response.headers["location"]).query)["state"][0]

    access_token = make_access_token(
        private_key, character_id=CHARACTER_ID, character_name="Alt Pilot"
    )
    respx.post(test_settings.eve_sso_token_url).mock(
        return_value=Response(
            200,
            json={
                "access_token": access_token,
                "refresh_token": "refresh-token-value",
                "expires_in": 1200,
            },
        )
    )
    respx.get(test_settings.eve_sso_jwks_url).mock(return_value=Response(200, json={"keys": [jwk]}))
    respx.get(f"{test_settings.esi_base_url}/characters/{CHARACTER_ID}/").mock(
        return_value=Response(200, json={"corporation_id": CORPORATION_ID})
    )

    client.get(
        "/auth/callback",
        params={"code": "auth-code", "state": state},
        follow_redirects=False,
    )


def _connect_corp(
    client: TestClient,
    test_settings: Settings,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
    *,
    character_id: int = CHARACTER_ID,
    character_name: str = "Alt Pilot",
) -> Response:
    private_key, jwk = rsa_key_pair
    connect_response = client.get("/auth/connect-corp", follow_redirects=False)
    state = parse_qs(urlparse(connect_response.headers["location"]).query)["state"][0]

    access_token = make_access_token(
        private_key, character_id=character_id, character_name=character_name
    )
    respx.post(test_settings.eve_sso_token_url).mock(
        return_value=Response(
            200,
            json={
                "access_token": access_token,
                "refresh_token": "corp-refresh-token-value",
                "expires_in": 1200,
            },
        )
    )
    respx.get(test_settings.eve_sso_jwks_url).mock(return_value=Response(200, json={"keys": [jwk]}))
    respx.get(f"{test_settings.esi_base_url}/characters/{character_id}/").mock(
        return_value=Response(200, json={"corporation_id": CORPORATION_ID})
    )

    # EVE SSO redirects back to the single registered callback URL for both flows
    # (see app/routes/auth.py) - hitting /auth/callback here, not a dedicated
    # connect-corp callback route, is what actually exercises that.
    return client.get(
        "/auth/callback",
        params={"code": "corp-auth-code", "state": state},
        follow_redirects=False,
    )


@respx.mock
def test_connect_corp_redirects_with_corp_scope(
    client: TestClient,
    test_settings: Settings,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    test_settings.eve_sso_corp_scopes = "esi-assets.read_corporation_assets.v1"
    _log_in(client, test_settings, rsa_key_pair)

    response = client.get("/auth/connect-corp", follow_redirects=False)

    assert response.status_code in (302, 307)
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["scope"] == ["esi-assets.read_corporation_assets.v1"]


@respx.mock
def test_connect_corp_callback_happy_path(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)

    response = _connect_corp(client, test_settings, rsa_key_pair)

    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/settings"

    respx.get(f"{test_settings.esi_base_url}/corporations/{CORPORATION_ID}/").mock(
        return_value=Response(200, json={"name": "Test Corp"})
    )
    respx.get(
        f"{test_settings.esi_base_url}/corporations/{CORPORATION_ID}/assets", params={"page": 1}
    ).mock(return_value=Response(200, headers={"X-Pages": "1"}, json=[]))
    respx.get(
        f"{test_settings.esi_base_url}/corporations/{CORPORATION_ID}/blueprints",
        params={"page": 1},
    ).mock(return_value=Response(200, headers={"X-Pages": "1"}, json=[]))
    respx.get(
        f"{test_settings.esi_base_url}/corporations/{CORPORATION_ID}/industry/jobs",
        params={"page": 1},
    ).mock(return_value=Response(200, headers={"X-Pages": "1"}, json=[]))

    settings_response = client.get("/settings")
    assert settings_response.status_code == 200
    assert "Test Corp" in settings_response.text
    assert "Disconnect" in settings_response.text


@respx.mock
def test_connect_corp_callback_rejects_mismatched_character(
    client: TestClient,
    test_settings: Settings,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)

    response = _connect_corp(
        client, test_settings, rsa_key_pair, character_id=999, character_name="Some Other Alt"
    )

    assert response.status_code == 400


@respx.mock
def test_disconnect_corp_clears_connection(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    _connect_corp(client, test_settings, rsa_key_pair)

    response = client.get("/auth/disconnect-corp", follow_redirects=False)
    assert response.status_code in (302, 307)

    settings_response = client.get("/settings")
    assert settings_response.status_code == 200
    assert "Connect corporation data" in settings_response.text
    assert "Disconnect" not in settings_response.text


@respx.mock
def test_settings_shows_not_connected_by_default(
    client: TestClient,
    test_settings: Settings,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)

    response = client.get("/settings")

    assert response.status_code == 200
    assert "Connect corporation data" in response.text


@respx.mock
def test_settings_shows_per_source_permission_status(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    _connect_corp(client, test_settings, rsa_key_pair)

    respx.get(f"{test_settings.esi_base_url}/corporations/{CORPORATION_ID}/").mock(
        return_value=Response(200, json={"name": "Test Corp"})
    )
    respx.get(
        f"{test_settings.esi_base_url}/corporations/{CORPORATION_ID}/assets", params={"page": 1}
    ).mock(
        return_value=Response(
            200,
            headers={"X-Pages": "1"},
            json=[
                {
                    "item_id": 1,
                    "type_id": TRITANIUM_TYPE_ID,
                    "location_id": 60003760,
                    "location_flag": "Hangar",
                    "location_type": "station",
                    "quantity": 100,
                    "is_singleton": False,
                }
            ],
        )
    )
    # No Director role: blueprints 403s.
    respx.get(
        f"{test_settings.esi_base_url}/corporations/{CORPORATION_ID}/blueprints",
        params={"page": 1},
    ).mock(return_value=Response(403, json={"error": "Character does not have required role(s)"}))
    respx.get(
        f"{test_settings.esi_base_url}/corporations/{CORPORATION_ID}/industry/jobs",
        params={"page": 1},
    ).mock(return_value=Response(200, headers={"X-Pages": "1"}, json=[]))

    response = client.get("/settings")

    assert response.status_code == 200
    assert "Connected &middot; 1 found" in response.text
    assert "No permission &mdash; requires the Director role" in response.text


@respx.mock
def test_settings_treats_401_as_no_permission_rather_than_crashing(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    # A corp token missing the scope an endpoint needs (e.g. EVE_SSO_CORP_SCOPES
    # was misconfigured when the character connected) gets a 401 from ESI, not a
    # 403 - the app should still degrade to "no permission" rather than 500.
    _log_in(client, test_settings, rsa_key_pair)
    _connect_corp(client, test_settings, rsa_key_pair)

    respx.get(f"{test_settings.esi_base_url}/corporations/{CORPORATION_ID}/").mock(
        return_value=Response(200, json={"name": "Test Corp"})
    )
    respx.get(
        f"{test_settings.esi_base_url}/corporations/{CORPORATION_ID}/assets", params={"page": 1}
    ).mock(return_value=Response(401, json={"error": "token is not valid for this endpoint"}))
    respx.get(
        f"{test_settings.esi_base_url}/corporations/{CORPORATION_ID}/blueprints",
        params={"page": 1},
    ).mock(return_value=Response(401, json={"error": "token is not valid for this endpoint"}))
    respx.get(
        f"{test_settings.esi_base_url}/corporations/{CORPORATION_ID}/industry/jobs",
        params={"page": 1},
    ).mock(return_value=Response(401, json={"error": "token is not valid for this endpoint"}))

    response = client.get("/settings")

    assert response.status_code == 200
    assert response.text.count("No permission &mdash;") == 3


@respx.mock
def test_nav_shows_settings_link_next_to_logout(
    client: TestClient,
    test_settings: Settings,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)

    response = client.get("/settings")

    assert response.status_code == 200
    nav_links_html = response.text[
        response.text.index('class="nav-links"') : response.text.index('class="nav-user"')
    ]
    nav_user_start = response.text.index('class="nav-user"')
    settings_link_pos = response.text.index('href="/settings"')
    logout_link_pos = response.text.index('href="/auth/logout"')
    # The Settings link sits in the nav-user block (right side, with the
    # avatar/logout), not in the middle nav-links content-section group.
    assert 'href="/settings"' not in nav_links_html
    assert nav_user_start < settings_link_pos < logout_link_pos


@respx.mock
def test_settings_shows_account_info(
    client: TestClient,
    test_settings: Settings,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)

    response = client.get("/settings")

    assert response.status_code == 200
    assert "Alt Pilot" in response.text
    assert f"Character ID {CHARACTER_ID}" in response.text
    assert "never fetched yet" in response.text


@respx.mock
async def test_settings_shows_populated_data_summary(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    await mongo_db.assets.insert_one(
        {
            "character_id": CHARACTER_ID,
            "cached_at": datetime(2026, 1, 1, 12, 0),
            "item_id": 1,
            "type_id": TRITANIUM_TYPE_ID,
            "location_id": 60003760,
            "location_flag": "Hangar",
            "location_type": "station",
            "quantity": 100,
            "is_singleton": False,
        }
    )

    response = client.get("/settings")

    assert response.status_code == 200
    assert "1 items" in response.text
    assert "2026-01-01 12:00 UTC" in response.text


@respx.mock
async def test_settings_refresh_clears_cached_data(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    await mongo_db.assets.insert_one(
        {
            "character_id": CHARACTER_ID,
            "cached_at": datetime(2026, 1, 1, 12, 0),
            "item_id": 1,
            "type_id": TRITANIUM_TYPE_ID,
            "location_id": 60003760,
            "location_flag": "Hangar",
            "location_type": "station",
            "quantity": 100,
            "is_singleton": False,
        }
    )

    response = client.get("/settings/refresh", follow_redirects=False)

    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/settings"
    assert await mongo_db.assets.count_documents({"character_id": CHARACTER_ID}) == 0


@respx.mock
async def test_settings_clear_data_deletes_character_and_personal_cache_only(
    client: TestClient,
    test_settings: Settings,
    mongo_db: AsyncMongoMockClient,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _log_in(client, test_settings, rsa_key_pair)
    _connect_corp(client, test_settings, rsa_key_pair)
    await mongo_db.assets.insert_one(
        {
            "character_id": CHARACTER_ID,
            "cached_at": datetime(2026, 1, 1, 12, 0),
            "item_id": 1,
            "type_id": TRITANIUM_TYPE_ID,
            "location_id": 60003760,
            "location_flag": "Hangar",
            "location_type": "station",
            "quantity": 100,
            "is_singleton": False,
        }
    )
    await mongo_db.corp_assets.insert_one(
        {
            "corporation_id": CORPORATION_ID,
            "cached_at": datetime(2026, 1, 1, 12, 0),
            "item_id": 2,
            "type_id": TRITANIUM_TYPE_ID,
            "location_id": 60003760,
            "location_flag": "Hangar",
            "location_type": "station",
            "quantity": 100,
            "is_singleton": False,
        }
    )

    response = client.get("/settings/clear-data", follow_redirects=False)

    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/"
    assert await mongo_db.characters.find_one({"_id": CHARACTER_ID}) is None
    assert await mongo_db.assets.count_documents({"character_id": CHARACTER_ID}) == 0
    # Corp-shared cache belongs to the whole corp, not just this character - untouched.
    assert await mongo_db.corp_assets.count_documents({"corporation_id": CORPORATION_ID}) == 1

    me_response = client.get("/auth/me")
    assert me_response.status_code == 401
