import hashlib
from datetime import UTC, datetime
from functools import lru_cache
from html import escape
from pathlib import Path

from app.services.locations import LocationInfo

_STATIC_DIR = Path(__file__).parent / "static"


@lru_cache
def _static_version(path: str) -> str:
    """Short content hash for a /static/... path, appended as a `?v=` query param so a new
    deploy's CSS isn't served stale from a layer that caches by URL alone (e.g. Cloudflare) -
    the URL only changes when the file's content does."""
    file_path = _STATIC_DIR / path.removeprefix("/static/")
    digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
    return digest[:8]


def static_url(path: str) -> str:
    return f"{path}?v={_static_version(path)}"


def gauge_color(percentage: float) -> str:
    if percentage >= 100:
        return "#3ddc84"
    if percentage >= 50:
        return "#f5c344"
    return "#f0625a"


def gauge_cell_html(
    percentage: float, value_text: str | None = None, *, owned_text: str | None = None
) -> str:
    """Renders a bar gauge. value_text (right of the bar) defaults to a percentage; pass
    owned_text too (e.g. for an "owned vs needed" comparison) to also show a fixed-width
    label to the left of the bar - both text slots are fixed-width so the bar itself is the
    same length on every row of a table, regardless of how many digits either value has."""
    clamped = min(100.0, max(0.0, percentage))
    color = gauge_color(percentage)
    text = value_text if value_text is not None else f"{percentage:.0f}%"
    owned_html = (
        f'<span class="mini-gauge-text mini-gauge-text-left">{owned_text}</span>'
        if owned_text is not None
        else ""
    )
    return f"""
      <div class="mini-gauge">
        {owned_html}
        <div class="mini-gauge-track">
          <div class="mini-gauge-fill" style="width: {clamped:.0f}%; background: {color};"></div>
        </div>
        <span class="mini-gauge-text">{text}</span>
      </div>
    """


def icon_url(type_id: int, is_copy: bool = False) -> str:
    variant = "bpc" if is_copy else "bp"
    return f"https://images.evetech.net/types/{type_id}/{variant}"


def item_icon_url(type_id: int) -> str:
    return f"https://images.evetech.net/types/{type_id}/icon"


_TRITANIUM_TYPE_ID = 34
FAVICON_URL = item_icon_url(_TRITANIUM_TYPE_ID)


def format_number(value: float) -> str:
    return f"{value:,.0f}"


def format_isk(value: float) -> str:
    abs_value = abs(value)
    if abs_value >= 1_000_000_000:
        return f"{value / 1_000_000_000:,.2f}B ISK"
    if abs_value >= 1_000_000:
        return f"{value / 1_000_000:,.2f}M ISK"
    if abs_value >= 1_000:
        return f"{value / 1_000:,.1f}K ISK"
    return f"{value:,.0f} ISK"


def format_duration(seconds: float) -> str:
    total_seconds = max(0, round(seconds))
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60

    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def security_status_color(security_status: float) -> str:
    # Standard EVE Online security-status color bands, high-sec (green/cyan) down to
    # low-sec (orange) - null-sec and negative statuses fall through to the same red.
    thresholds = (
        (1.0, "#2fefef"),
        (0.9, "#48f0c0"),
        (0.8, "#00ef47"),
        (0.7, "#00f000"),
        (0.6, "#8fef2f"),
        (0.5, "#efef00"),
        (0.4, "#d77700"),
        (0.3, "#f06000"),
        (0.2, "#f04000"),
        (0.1, "#f00000"),
    )
    for threshold, color in thresholds:
        if security_status >= threshold:
            return color
    return "#f00000"


def _rounded_security_status(security_status: float) -> float:
    rounded = round(security_status, 1)
    return 0.0 if rounded == 0 else rounded  # avoid rendering "-0.0" for values that round to zero


def security_status_html(security_status: float) -> str:
    rounded = _rounded_security_status(security_status)
    color = security_status_color(rounded)
    return f'<span style="color: {color};">{rounded:.1f}</span>'


def location_label_html(location_id: int, info: LocationInfo | None) -> str:
    label = escape(info.name) if info and info.name else escape(f"Location {location_id}")
    if info is None or info.security_status is None:
        return label
    return f"{label} ({security_status_html(info.security_status)})"


def location_label_text(location_id: int, info: LocationInfo | None) -> str:
    """Same as location_label_html, but plain text - for contexts like <option> that can't
    render markup."""
    label = info.name if info and info.name else f"Location {location_id}"
    if info is None or info.security_status is None:
        return label
    return f"{label} ({_rounded_security_status(info.security_status):.1f})"


def humanize_relative_time(target: datetime) -> str:
    seconds = (target - datetime.now(UTC)).total_seconds()
    if seconds <= 0:
        return "any moment"

    days, remainder = divmod(int(seconds), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60

    if days > 0:
        value, unit = days, "day"
    elif hours > 0:
        value, unit = hours, "hour"
    elif minutes > 0:
        value, unit = minutes, "minute"
    else:
        return "in less than a minute"

    return f"in {value} {unit}{'s' if value != 1 else ''}"
