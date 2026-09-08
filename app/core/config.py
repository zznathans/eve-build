from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    eve_sso_client_id: str = ""
    eve_sso_callback_url: str = ""
    eve_sso_scopes: str = ""
    eve_sso_corp_scopes: str = ""
    eve_sso_authorize_url: str = "https://login.eveonline.com/v2/oauth/authorize"
    eve_sso_token_url: str = "https://login.eveonline.com/v2/oauth/token"
    eve_sso_jwks_url: str = "https://login.eveonline.com/oauth/jwks"
    eve_sso_issuer: str = "https://login.eveonline.com"
    eve_sso_audience: str = "EVE Online"

    esi_base_url: str = "https://esi.evetech.net"
    esi_compatibility_date: str = "2026-08-18"
    esi_user_agent: str = "eve-build"

    market_prices_refresh_api_key: str = ""

    market_orders_chunk_size: int = 2000
    market_orders_page_retry_max_attempts: int = 5
    market_orders_error_limit_threshold: int = 10
    market_orders_write_prefetch: int = 10

    mongodb_uri: str = "mongodb://localhost:27017"
    mongodb_database: str = "eve-build"
    mongodb_max_pool_size: int = 5

    sde_data_dir: str = "app/data/sde"
    run_migrations_on_startup: bool = True

    mongo_indexes_dir: str = "app/config/mongo_indexes"
    sync_indexes_on_startup: bool = True

    startup_lock_ttl_seconds: int = 480
    startup_lock_poll_interval_seconds: float = 2.0
    startup_lock_wait_timeout_seconds: int = 420

    redis_enabled: bool = False
    redis_url: str = "redis://localhost:6379/0"
    redis_cache_ttl_seconds: int = 60 * 60 * 24
    redis_max_connections: int = 5

    rabbitmq_enabled: bool = False
    rabbitmq_url: str = "amqp://guest:guest@localhost:5672/"

    metrics_enabled: bool = False
    metrics_db_gauges_enabled: bool = True
    metrics_gauge_refresh_seconds: int = 300

    session_secret_key: str = "insecure-dev-secret-change-me"
    session_cookie_name: str = "eve_build_session"
    session_max_age_seconds: int = 60 * 60 * 24 * 14


@lru_cache
def get_settings() -> Settings:
    return Settings()
