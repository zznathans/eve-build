import asyncio
import random
from dataclasses import dataclass
from time import monotonic
from typing import Any

import httpx
from prometheus_client import Counter, Histogram

from app.core.config import Settings

_STATION_ID_MAX = 64_000_000_000

# ESI applies IP-wide error-rate limiting (not a plain per-route request limit) via the
# X-Esi-Error-Limit-Remain/-Reset headers, and returns 420/429/5xx on transient overload -
# retried with backoff since a full market-order scrape makes thousands of requests.
_RETRYABLE_STATUS_CODES = frozenset({420, 429, 500, 502, 503, 504})
_RETRY_BACKOFF_BASE_SECONDS = 1.0
_RETRY_BACKOFF_MAX_SECONDS = 30.0

ESI_REQUEST_DURATION = Histogram(
    "eve_build_esi_request_duration_seconds", "ESI HTTP request duration", ["endpoint"]
)
ESI_REQUEST_ERRORS = Counter(
    "eve_build_esi_request_errors_total", "ESI HTTP requests that raised an error", ["endpoint"]
)


async def _timed_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    endpoint: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    start = monotonic()
    try:
        response = await client.get(url, params=params, headers=headers)
        response.raise_for_status()
        return response
    except httpx.HTTPError:
        ESI_REQUEST_ERRORS.labels(endpoint=endpoint).inc()
        raise
    finally:
        ESI_REQUEST_DURATION.labels(endpoint=endpoint).observe(monotonic() - start)


@dataclass(frozen=True)
class BlueprintEntry:
    item_id: int
    type_id: int
    location_id: int
    location_flag: str
    quantity: int
    runs: int
    material_efficiency: int
    time_efficiency: int


@dataclass(frozen=True)
class AssetEntry:
    item_id: int
    type_id: int
    location_id: int
    location_flag: str
    location_type: str
    quantity: int
    is_singleton: bool


@dataclass(frozen=True)
class IndustryJobEntry:
    job_id: int
    activity_id: int
    blueprint_id: int
    blueprint_type_id: int
    product_type_id: int | None
    facility_id: int
    runs: int
    status: str
    start_date: str
    end_date: str


async def _sleep_before_retry(response: httpx.Response, attempt: int) -> None:
    retry_after = response.headers.get("Retry-After")
    if retry_after is not None:
        try:
            delay = float(retry_after)
        except ValueError:
            delay = _RETRY_BACKOFF_BASE_SECONDS
    else:
        delay = min(_RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), _RETRY_BACKOFF_MAX_SECONDS)
        delay += random.uniform(0, 1)
    await asyncio.sleep(delay)


async def _respect_error_limit(response: httpx.Response, settings: Settings) -> None:
    remain = response.headers.get("X-Esi-Error-Limit-Remain")
    reset = response.headers.get("X-Esi-Error-Limit-Reset")
    if remain is None or reset is None:
        return
    if int(remain) <= settings.market_orders_error_limit_threshold:
        await asyncio.sleep(float(reset))


async def _get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    endpoint: str,
    settings: Settings,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    attempt = 0
    while True:
        attempt += 1
        try:
            response = await _timed_get(
                client, url, endpoint=endpoint, params=params, headers=headers
            )
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if (
                status_code not in _RETRYABLE_STATUS_CODES
                or attempt >= settings.market_orders_page_retry_max_attempts
            ):
                raise
            await _sleep_before_retry(exc.response, attempt)
            continue

        await _respect_error_limit(response, settings)
        return response


def _headers(settings: Settings, access_token: str | None) -> dict[str, str]:
    headers = {
        "X-Compatibility-Date": settings.esi_compatibility_date,
        "User-Agent": settings.esi_user_agent,
    }
    if access_token is not None:
        headers["Authorization"] = f"Bearer {access_token}"
    return headers


async def _get_all_pages(
    settings: Settings, access_token: str, path: str, *, endpoint: str
) -> list[dict[str, Any]]:
    url = f"{settings.esi_base_url}{path}"
    headers = _headers(settings, access_token)

    async with httpx.AsyncClient() as client:
        first_response = await _timed_get(
            client, url, endpoint=endpoint, params={"page": 1}, headers=headers
        )
        results: list[dict[str, Any]] = list(first_response.json())
        total_pages = int(first_response.headers.get("X-Pages", "1"))

        for page in range(2, total_pages + 1):
            response = await _timed_get(
                client, url, endpoint=endpoint, params={"page": page}, headers=headers
            )
            results.extend(response.json())

    return results


async def get_character_blueprints(
    settings: Settings, access_token: str, character_id: int
) -> list[BlueprintEntry]:
    raw_entries = await _get_all_pages(
        settings,
        access_token,
        f"/characters/{character_id}/blueprints",
        endpoint="characters/blueprints",
    )
    return [
        BlueprintEntry(
            item_id=entry["item_id"],
            type_id=entry["type_id"],
            location_id=entry["location_id"],
            location_flag=entry["location_flag"],
            quantity=entry["quantity"],
            runs=entry["runs"],
            material_efficiency=entry["material_efficiency"],
            time_efficiency=entry["time_efficiency"],
        )
        for entry in raw_entries
    ]


async def get_character_assets(
    settings: Settings, access_token: str, character_id: int
) -> list[AssetEntry]:
    raw_entries = await _get_all_pages(
        settings,
        access_token,
        f"/characters/{character_id}/assets",
        endpoint="characters/assets",
    )
    return [
        AssetEntry(
            item_id=entry["item_id"],
            type_id=entry["type_id"],
            location_id=entry["location_id"],
            location_flag=entry["location_flag"],
            location_type=entry["location_type"],
            quantity=entry["quantity"],
            is_singleton=entry["is_singleton"],
        )
        for entry in raw_entries
    ]


async def get_character_public_info(settings: Settings, character_id: int) -> int:
    """Returns the character's corporation_id. Unauthenticated - ESI's
    /characters/{character_id}/ endpoint is public."""
    url = f"{settings.esi_base_url}/characters/{character_id}/"
    headers = _headers(settings, None)

    async with httpx.AsyncClient() as client:
        response = await _timed_get(client, url, endpoint="characters/public_info", headers=headers)

    return int(response.json()["corporation_id"])


async def get_corporation_name(settings: Settings, corporation_id: int) -> str | None:
    """Unauthenticated - ESI's /corporations/{corporation_id}/ endpoint is public."""
    url = f"{settings.esi_base_url}/corporations/{corporation_id}/"
    headers = _headers(settings, None)

    async with httpx.AsyncClient() as client:
        try:
            response = await _timed_get(
                client, url, endpoint="corporations/public_info", headers=headers
            )
        except httpx.HTTPStatusError:
            return None

    name = response.json().get("name")
    return name if isinstance(name, str) else None


async def get_corporation_assets(
    settings: Settings, access_token: str, corporation_id: int
) -> list[AssetEntry]:
    raw_entries = await _get_all_pages(
        settings,
        access_token,
        f"/corporations/{corporation_id}/assets",
        endpoint="corporations/assets",
    )
    return [
        AssetEntry(
            item_id=entry["item_id"],
            type_id=entry["type_id"],
            location_id=entry["location_id"],
            location_flag=entry["location_flag"],
            location_type=entry["location_type"],
            quantity=entry["quantity"],
            is_singleton=entry["is_singleton"],
        )
        for entry in raw_entries
    ]


async def get_corporation_blueprints(
    settings: Settings, access_token: str, corporation_id: int
) -> list[BlueprintEntry]:
    raw_entries = await _get_all_pages(
        settings,
        access_token,
        f"/corporations/{corporation_id}/blueprints",
        endpoint="corporations/blueprints",
    )
    return [
        BlueprintEntry(
            item_id=entry["item_id"],
            type_id=entry["type_id"],
            location_id=entry["location_id"],
            location_flag=entry["location_flag"],
            quantity=entry["quantity"],
            runs=entry["runs"],
            material_efficiency=entry["material_efficiency"],
            time_efficiency=entry["time_efficiency"],
        )
        for entry in raw_entries
    ]


async def get_corporation_industry_jobs(
    settings: Settings, access_token: str, corporation_id: int
) -> list[IndustryJobEntry]:
    raw_entries = await _get_all_pages(
        settings,
        access_token,
        f"/corporations/{corporation_id}/industry/jobs",
        endpoint="corporations/industry_jobs",
    )
    return [
        IndustryJobEntry(
            job_id=entry["job_id"],
            activity_id=entry["activity_id"],
            blueprint_id=entry["blueprint_id"],
            blueprint_type_id=entry["blueprint_type_id"],
            product_type_id=entry.get("product_type_id"),
            facility_id=entry["facility_id"],
            runs=entry["runs"],
            status=entry["status"],
            start_date=entry["start_date"],
            end_date=entry["end_date"],
        )
        for entry in raw_entries
    ]


async def get_character_industry_jobs(
    settings: Settings, access_token: str, character_id: int
) -> list[IndustryJobEntry]:
    url = f"{settings.esi_base_url}/characters/{character_id}/industry/jobs"
    headers = _headers(settings, access_token)

    async with httpx.AsyncClient() as client:
        response = await _timed_get(
            client, url, endpoint="characters/industry_jobs", headers=headers
        )

    return [
        IndustryJobEntry(
            job_id=entry["job_id"],
            activity_id=entry["activity_id"],
            blueprint_id=entry["blueprint_id"],
            blueprint_type_id=entry["blueprint_type_id"],
            product_type_id=entry.get("product_type_id"),
            facility_id=entry["facility_id"],
            runs=entry["runs"],
            status=entry["status"],
            start_date=entry["start_date"],
            end_date=entry["end_date"],
        )
        for entry in response.json()
    ]


@dataclass(frozen=True)
class ColonyEntry:
    planet_id: int
    solar_system_id: int
    planet_type: str
    owner_id: int
    last_update: str
    upgrade_level: int
    num_pins: int


@dataclass(frozen=True)
class ColonyDetailEntry:
    pins: list[dict[str, Any]]
    links: list[dict[str, Any]]
    routes: list[dict[str, Any]]


@dataclass(frozen=True)
class ColonyRecord:
    """A colony's list-level summary merged with its per-planet detail, so the whole
    thing can be cached as one flat record via esi_cache.cached_character_list."""

    planet_id: int
    solar_system_id: int
    planet_type: str
    owner_id: int
    last_update: str
    upgrade_level: int
    num_pins: int
    pins: list[dict[str, Any]]
    links: list[dict[str, Any]]
    routes: list[dict[str, Any]]


async def get_character_colonies(
    settings: Settings, access_token: str, character_id: int
) -> list[ColonyEntry]:
    url = f"{settings.esi_base_url}/characters/{character_id}/planets/"
    headers = _headers(settings, access_token)

    async with httpx.AsyncClient() as client:
        response = await _timed_get(client, url, endpoint="characters/planets", headers=headers)

    return [
        ColonyEntry(
            planet_id=entry["planet_id"],
            solar_system_id=entry["solar_system_id"],
            planet_type=entry["planet_type"],
            owner_id=entry["owner_id"],
            last_update=entry["last_update"],
            upgrade_level=entry["upgrade_level"],
            num_pins=entry["num_pins"],
        )
        for entry in response.json()
    ]


async def get_character_colony_detail(
    settings: Settings, access_token: str, character_id: int, planet_id: int
) -> ColonyDetailEntry:
    url = f"{settings.esi_base_url}/characters/{character_id}/planets/{planet_id}/"
    headers = _headers(settings, access_token)

    async with httpx.AsyncClient() as client:
        response = await _get_with_retry(
            client, url, endpoint="characters/planet_detail", settings=settings, headers=headers
        )

    data = response.json()
    return ColonyDetailEntry(
        pins=data.get("pins", []),
        links=data.get("links", []),
        routes=data.get("routes", []),
    )


async def get_planet_name(settings: Settings, planet_id: int) -> str | None:
    """Unauthenticated - ESI's /universe/planets/{planet_id}/ endpoint is public."""
    url = f"{settings.esi_base_url}/universe/planets/{planet_id}"
    headers = _headers(settings, None)

    async with httpx.AsyncClient() as client:
        try:
            response = await _timed_get(client, url, endpoint="universe/planets", headers=headers)
        except httpx.HTTPStatusError:
            return None

    name = response.json().get("name")
    return name if isinstance(name, str) else None


@dataclass(frozen=True)
class MarketPriceEntry:
    type_id: int
    adjusted_price: float | None
    average_price: float | None


async def get_market_prices(settings: Settings) -> list[MarketPriceEntry]:
    url = f"{settings.esi_base_url}/markets/prices"
    headers = _headers(settings, None)

    async with httpx.AsyncClient() as client:
        response = await _timed_get(client, url, endpoint="markets/prices", headers=headers)

    return [
        MarketPriceEntry(
            type_id=entry["type_id"],
            adjusted_price=entry.get("adjusted_price"),
            average_price=entry.get("average_price"),
        )
        for entry in response.json()
    ]


async def get_region_ids(settings: Settings) -> list[int]:
    """Unauthenticated - ESI's /universe/regions/ endpoint is public. Includes a handful of
    wormhole/void regions that have no market (see get_market_orders_page's 404 handling)."""
    url = f"{settings.esi_base_url}/universe/regions/"
    headers = _headers(settings, None)

    async with httpx.AsyncClient() as client:
        response = await _get_with_retry(
            client, url, endpoint="universe/regions", settings=settings, headers=headers
        )

    return [int(region_id) for region_id in response.json()]


@dataclass(frozen=True)
class MarketOrderEntry:
    order_id: int
    type_id: int
    location_id: int
    is_buy_order: bool
    price: float
    volume_remain: int
    volume_total: int
    min_volume: int
    duration: int
    issued: str
    range: str


async def get_market_orders_page(
    settings: Settings, region_id: int, page: int
) -> tuple[list[MarketOrderEntry], int]:
    """Fetches one page of /markets/{region_id}/orders/. Returns ([], 0) for regions with no
    market (e.g. wormhole regions), which ESI reports as a 404, rather than raising."""
    url = f"{settings.esi_base_url}/markets/{region_id}/orders/"
    headers = _headers(settings, None)

    async with httpx.AsyncClient() as client:
        try:
            response = await _get_with_retry(
                client,
                url,
                endpoint="markets/orders",
                settings=settings,
                params={"page": page},
                headers=headers,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return [], 0
            raise

    total_pages = int(response.headers.get("X-Pages", "1"))
    entries = [
        MarketOrderEntry(
            order_id=entry["order_id"],
            type_id=entry["type_id"],
            location_id=entry["location_id"],
            is_buy_order=entry["is_buy_order"],
            price=entry["price"],
            volume_remain=entry["volume_remain"],
            volume_total=entry["volume_total"],
            min_volume=entry["min_volume"],
            duration=entry["duration"],
            issued=entry["issued"],
            range=entry["range"],
        )
        for entry in response.json()
    ]
    return entries, total_pages


@dataclass(frozen=True)
class LocationDetails:
    name: str | None
    system_id: int | None


async def get_location_details(
    settings: Settings, access_token: str, location_id: int
) -> LocationDetails:
    if location_id < _STATION_ID_MAX:
        path = f"/universe/stations/{location_id}"
        headers = _headers(settings, None)
        endpoint = "universe/stations"
    else:
        path = f"/universe/structures/{location_id}"
        headers = _headers(settings, access_token)
        endpoint = "universe/structures"

    async with httpx.AsyncClient() as client:
        try:
            response = await _timed_get(
                client, f"{settings.esi_base_url}{path}", endpoint=endpoint, headers=headers
            )
        except httpx.HTTPStatusError:
            return LocationDetails(name=None, system_id=None)

    data = response.json()
    name = data.get("name")
    system_id = data.get("system_id")
    return LocationDetails(
        name=name if isinstance(name, str) else None,
        system_id=system_id if isinstance(system_id, int) else None,
    )


async def get_system_security_status(settings: Settings, system_id: int) -> float | None:
    headers = _headers(settings, None)
    async with httpx.AsyncClient() as client:
        try:
            response = await _timed_get(
                client,
                f"{settings.esi_base_url}/universe/systems/{system_id}",
                endpoint="universe/systems",
                headers=headers,
            )
        except httpx.HTTPStatusError:
            return None

    security_status = response.json().get("security_status")
    return float(security_status) if isinstance(security_status, int | float) else None


async def get_system_name(settings: Settings, system_id: int) -> str | None:
    headers = _headers(settings, None)
    async with httpx.AsyncClient() as client:
        try:
            response = await _timed_get(
                client,
                f"{settings.esi_base_url}/universe/systems/{system_id}",
                endpoint="universe/systems",
                headers=headers,
            )
        except httpx.HTTPStatusError:
            return None

    name = response.json().get("name")
    return name if isinstance(name, str) else None
