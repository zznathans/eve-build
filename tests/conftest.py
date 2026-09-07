import base64
from collections.abc import Iterator

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fakeredis.aioredis import FakeRedis
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from app.core.config import Settings, get_settings
from app.db.mongo import get_database
from app.db.rabbitmq import get_rabbitmq
from app.db.redis import get_redis
from app.main import app

TEST_KEY_ID = "test-key-1"


class _FakeExchange:
    def __init__(self, connection: "FakeRabbitMQConnection") -> None:
        self._connection = connection

    async def publish(self, message: object, routing_key: str) -> None:
        body = getattr(message, "body", message)
        self._connection.published.append((routing_key, body))


class _FakeChannel:
    def __init__(self, connection: "FakeRabbitMQConnection") -> None:
        self._connection = connection
        self.default_exchange = _FakeExchange(connection)

    async def declare_queue(self, name: str, durable: bool = True) -> None:
        return None

    async def close(self) -> None:
        return None


class FakeRabbitMQConnection:
    """Minimal stand-in for aio_pika's AbstractRobustConnection - enough to exercise route/
    dispatch code that opens a channel and publishes, without a real broker. `published`
    records every (routing_key, body) pair passed to `channel.default_exchange.publish`."""

    def __init__(self) -> None:
        self.closed = False
        self.published: list[tuple[str, bytes]] = []

    async def close(self) -> None:
        self.closed = True

    async def channel(self) -> _FakeChannel:
        return _FakeChannel(self)


@pytest.fixture
def rsa_key_pair() -> tuple[rsa.RSAPrivateKey, dict[str, object]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_numbers = private_key.public_key().public_numbers()

    def _int_to_b64url(value: int) -> str:
        length = (value.bit_length() + 7) // 8
        return base64.urlsafe_b64encode(value.to_bytes(length, "big")).decode("ascii").rstrip("=")

    jwk = {
        "kty": "RSA",
        "kid": TEST_KEY_ID,
        "use": "sig",
        "alg": "RS256",
        "n": _int_to_b64url(public_numbers.n),
        "e": _int_to_b64url(public_numbers.e),
    }
    return private_key, jwk


@pytest.fixture
def mongo_db() -> object:
    client = AsyncMongoMockClient()
    return client["eve-build"]


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def fake_rabbitmq() -> FakeRabbitMQConnection:
    return FakeRabbitMQConnection()


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        eve_sso_client_id="test-client-id",
        eve_sso_callback_url="http://testserver/auth/callback",
        eve_sso_scopes="",
        session_secret_key="test-secret",
        mongodb_uri="mongodb://localhost:27017",
        mongodb_database="eve-build",
    )


@pytest.fixture
def client(
    mongo_db: object, test_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    # `lifespan` calls get_settings() directly (not via Depends), so dependency_overrides
    # can't reach it — disable the startup migration and Redis/RabbitMQ via env vars instead,
    # since they'd otherwise hit the real local MongoDB/Redis/RabbitMQ (e.g. if
    # REDIS_ENABLED=true in a developer's .env, every test would silently read/write the real
    # Redis instance).
    monkeypatch.setenv("RUN_MIGRATIONS_ON_STARTUP", "false")
    monkeypatch.setenv("SYNC_INDEXES_ON_STARTUP", "false")
    monkeypatch.setenv("REDIS_ENABLED", "false")
    monkeypatch.setenv("RABBITMQ_ENABLED", "false")
    get_settings.cache_clear()
    app.dependency_overrides[get_database] = lambda: mongo_db
    app.dependency_overrides[get_settings] = lambda: test_settings
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.fixture
def client_with_redis(client: TestClient, fake_redis: FakeRedis) -> TestClient:
    app.dependency_overrides[get_redis] = lambda: fake_redis
    return client


@pytest.fixture
def client_with_rabbitmq(client: TestClient, fake_rabbitmq: FakeRabbitMQConnection) -> TestClient:
    app.dependency_overrides[get_rabbitmq] = lambda: fake_rabbitmq
    return client


def make_access_token(
    private_key: rsa.RSAPrivateKey,
    *,
    character_id: int = 12345,
    character_name: str = "Test Character",
    owner_hash: str = "test-owner-hash",
    scopes: list[str] | None = None,
    issuer: str = "https://login.eveonline.com",
    audience: str = "EVE Online",
) -> str:
    claims = {
        "sub": f"CHARACTER:EVE:{character_id}",
        "name": character_name,
        "owner": owner_hash,
        "scp": scopes or [],
        "iss": issuer,
        "aud": audience,
    }
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": TEST_KEY_ID})
