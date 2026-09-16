from urllib.parse import parse_qs, urlparse

import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from httpx import Response

from app.core.config import Settings
from tests.conftest import make_access_token


def test_login_redirects_to_eve_authorize_url(client: TestClient) -> None:
    response = client.get("/auth/login", follow_redirects=False)

    assert response.status_code in (302, 307)
    location = response.headers["location"]
    parsed = urlparse(location)
    assert parsed.netloc == "login.eveonline.com"
    query = parse_qs(parsed.query)
    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    assert "state" in query
    assert "code_challenge" in query
    assert client.cookies.get("eve_build_session") is not None


@respx.mock
def test_callback_happy_path(
    client: TestClient,
    test_settings: Settings,
    mongo_db: object,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    private_key, jwk = rsa_key_pair
    login_response = client.get("/auth/login", follow_redirects=False)
    state = parse_qs(urlparse(login_response.headers["location"]).query)["state"][0]

    access_token = make_access_token(private_key, character_id=555, character_name="Alt Pilot")
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
    respx.get(f"{test_settings.esi_base_url}/characters/555/").mock(
        return_value=Response(200, json={"corporation_id": 98000001})
    )

    callback_response = client.get(
        "/auth/callback",
        params={"code": "auth-code", "state": state},
        follow_redirects=False,
    )

    assert callback_response.status_code in (302, 307)

    me_response = client.get("/auth/me")
    assert me_response.status_code == 200
    assert me_response.json() == {
        "character_id": 555,
        "character_name": "Alt Pilot",
        "scopes": [],
    }

    respx.get(f"{test_settings.esi_base_url}/characters/555/blueprints", params={"page": 1}).mock(
        return_value=Response(200, headers={"X-Pages": "1"}, json=[])
    )
    respx.get(f"{test_settings.esi_base_url}/characters/555/assets", params={"page": 1}).mock(
        return_value=Response(200, headers={"X-Pages": "1"}, json=[])
    )
    respx.get(f"{test_settings.esi_base_url}/characters/555/industry/jobs").mock(
        return_value=Response(200, json=[])
    )

    root_response = client.get("/")
    assert root_response.status_code == 200
    assert "Alt Pilot" in root_response.text
    assert "Log out" in root_response.text
    assert "https://images.evetech.net/characters/555/portrait" in root_response.text


def test_callback_rejects_missing_state(client: TestClient) -> None:
    response = client.get("/auth/callback", params={"code": "auth-code", "state": "bogus"})
    assert response.status_code == 400


def test_me_unauthenticated_returns_401(client: TestClient) -> None:
    response = client.get("/auth/me")
    assert response.status_code == 401


@respx.mock
def test_logout_clears_session(
    client: TestClient,
    test_settings: Settings,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    private_key, jwk = rsa_key_pair
    login_response = client.get("/auth/login", follow_redirects=False)
    state = parse_qs(urlparse(login_response.headers["location"]).query)["state"][0]

    access_token = make_access_token(private_key, character_id=1, character_name="Someone")
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
    respx.get(f"{test_settings.esi_base_url}/characters/1/").mock(
        return_value=Response(200, json={"corporation_id": 98000001})
    )
    client.get(
        "/auth/callback",
        params={"code": "auth-code", "state": state},
        follow_redirects=False,
    )
    assert client.get("/auth/me").status_code == 200

    client.get("/auth/logout", follow_redirects=False)

    assert client.get("/auth/me").status_code == 401


@respx.mock
def test_callback_rejects_character_not_on_whitelist(
    client: TestClient,
    test_settings: Settings,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    test_settings.access_whitelist_character_ids = "999"

    private_key, jwk = rsa_key_pair
    login_response = client.get("/auth/login", follow_redirects=False)
    state = parse_qs(urlparse(login_response.headers["location"]).query)["state"][0]

    access_token = make_access_token(private_key, character_id=555, character_name="Alt Pilot")
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
    respx.get(f"{test_settings.esi_base_url}/characters/555/").mock(
        return_value=Response(200, json={"corporation_id": 98000001})
    )

    callback_response = client.get(
        "/auth/callback",
        params={"code": "auth-code", "state": state},
        follow_redirects=False,
    )

    assert callback_response.status_code == 403
    assert client.get("/auth/me").status_code == 401


@respx.mock
def test_callback_allows_character_via_corporation_whitelist(
    client: TestClient,
    test_settings: Settings,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    test_settings.access_whitelist_corporation_ids = "98000001"

    private_key, jwk = rsa_key_pair
    login_response = client.get("/auth/login", follow_redirects=False)
    state = parse_qs(urlparse(login_response.headers["location"]).query)["state"][0]

    access_token = make_access_token(private_key, character_id=555, character_name="Alt Pilot")
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
    respx.get(f"{test_settings.esi_base_url}/characters/555/").mock(
        return_value=Response(200, json={"corporation_id": 98000001})
    )

    callback_response = client.get(
        "/auth/callback",
        params={"code": "auth-code", "state": state},
        follow_redirects=False,
    )

    assert callback_response.status_code in (302, 307)
    assert client.get("/auth/me").status_code == 200


@respx.mock
def test_already_logged_in_character_is_logged_out_once_whitelist_excludes_them(
    client: TestClient,
    test_settings: Settings,
    rsa_key_pair: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    private_key, jwk = rsa_key_pair
    login_response = client.get("/auth/login", follow_redirects=False)
    state = parse_qs(urlparse(login_response.headers["location"]).query)["state"][0]

    access_token = make_access_token(private_key, character_id=555, character_name="Alt Pilot")
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
    respx.get(f"{test_settings.esi_base_url}/characters/555/").mock(
        return_value=Response(200, json={"corporation_id": 98000001})
    )

    client.get(
        "/auth/callback",
        params={"code": "auth-code", "state": state},
        follow_redirects=False,
    )
    assert client.get("/auth/me").status_code == 200

    # Whitelist tightened after login, to a corporation the cached home_corporation_id
    # (98000001) isn't in - the session should be revoked on the very next request,
    # without another SSO round trip or ESI call.
    test_settings.access_whitelist_corporation_ids = "98000002"

    assert client.get("/auth/me").status_code == 401
