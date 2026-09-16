from app.services.locations import LocationInfo
from app.web import (
    location_label_html,
    location_label_text,
    security_status_color,
    security_status_html,
)


def test_security_status_color_bands_match_eve_convention() -> None:
    assert security_status_color(1.0) == "#2fefef"
    assert security_status_color(0.9) == "#48f0c0"
    assert security_status_color(0.5) == "#efef00"
    assert security_status_color(0.45) == "#d77700"
    assert security_status_color(0.05) == "#f00000"
    assert security_status_color(0.0) == "#f00000"
    assert security_status_color(-0.9) == "#f00000"


def test_security_status_html_rounds_to_one_decimal() -> None:
    # 0.9459991455078125 (Jita's real security status) rounds to 0.9
    assert security_status_html(0.9459991455078125) == '<span style="color: #48f0c0;">0.9</span>'


def test_security_status_html_avoids_negative_zero() -> None:
    assert security_status_html(-0.04) == '<span style="color: #f00000;">0.0</span>'


def test_location_label_html_includes_colored_security_status() -> None:
    info = LocationInfo(name="Jita IV - Moon 4", security_status=0.9459991455078125)

    label = location_label_html(60003760, info)

    assert label == ('Jita IV - Moon 4 (<span style="color: #48f0c0;">0.9</span>)')


def test_location_label_html_omits_security_status_when_unknown() -> None:
    info = LocationInfo(name="Jita IV - Moon 4", security_status=None)

    assert location_label_html(60003760, info) == "Jita IV - Moon 4"


def test_location_label_html_falls_back_when_info_missing() -> None:
    assert location_label_html(60003760, None) == "Location 60003760"


def test_location_label_html_escapes_name() -> None:
    info = LocationInfo(name="<script>alert(1)</script>", security_status=None)

    assert "<script>" not in location_label_html(60003760, info)


def test_location_label_text_is_plain_text() -> None:
    info = LocationInfo(name="Jita IV - Moon 4", security_status=0.9459991455078125)

    assert location_label_text(60003760, info) == "Jita IV - Moon 4 (0.9)"
