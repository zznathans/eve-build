from pathlib import Path

from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app import web

_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(),
    trim_blocks=True,
    lstrip_blocks=True,
)
templates = Jinja2Templates(env=_env)
templates.env.filters["format_number"] = web.format_number
templates.env.filters["format_isk"] = web.format_isk
templates.env.filters["humanize_relative_time"] = web.humanize_relative_time
templates.env.filters["item_icon_url"] = web.item_icon_url
templates.env.filters["icon_url"] = web.icon_url
templates.env.filters["security_status_color"] = web.security_status_color
templates.env.filters["gauge_color"] = web.gauge_color
templates.env.globals["static_url"] = web.static_url
templates.env.globals["FAVICON_URL"] = web.FAVICON_URL
