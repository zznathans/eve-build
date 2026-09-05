from datetime import UTC, datetime
from html import escape
import re
from typing import cast

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse, RedirectResponse
from motor.motor_asyncio import AsyncIOMotorDatabase
from redis.asyncio import Redis

from app.core.config import Settings, get_settings
from app.db.mongo import get_database
from app.db.redis import get_redis
from app.deps import get_current_character
from app.models.character import CharacterDocument
from app.services import build_chain, plan, sde
from app.web import format_isk, item_icon_url, render_page, section_html, summary_stat_html

router = APIRouter(prefix="/plans", tags=["plans"])

_PLAN_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_LIST_STYLE = ["/static/card.css", "/static/build.css"]
_DETAIL_STYLE = ["/static/card.css", "/static/build-detail.css"]


def _format_timestamp(value: datetime) -> str:
    return value.replace(tzinfo=UTC).strftime("%Y-%m-%d %H:%M UTC")


def _materials_table(materials: list[build_chain.RawMaterial]) -> str:
    rows = "".join(f"""
          <tr>
            <td>{escape(material.name)}</td>
            <td>{material.quantity}</td>
            <td>{format_isk(material.quantity * material.unit_price)}</td>
          </tr>
        """ for material in materials)
    return f"""
      <table class="mini-table">
        <tbody>{rows}</tbody>
      </table>
    """


@router.get("", response_class=HTMLResponse)
async def list_plans(
    character: CharacterDocument = Depends(get_current_character),
    db: AsyncIOMotorDatabase = Depends(get_database),
    redis: Redis | None = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    plans = await plan.list_plans(db, character.character_id)

    if not plans:
        body = (
            '<div class="page"><h1>Plans</h1>'
            '<p class="empty">No plans saved yet - build something and add it to a plan.</p></div>'
        )
        return HTMLResponse(render_page("Plans", body, _LIST_STYLE, character=character))

    target_type_ids = {
        cast(int, cast(list[dict[str, object]], doc["jobs"])[0]["target_type_id"]) for doc in plans
    }
    type_docs = await sde.type_docs(db, redis, settings, target_type_ids)

    def _name(type_id: int) -> str:
        return str(type_docs.get(type_id, {}).get("name", f"Type {type_id}"))

    cards = ""
    for doc in plans:
        jobs = cast(list[dict[str, object]], doc["jobs"])
        first_job_type_id = cast(int, jobs[0]["target_type_id"])
        jobs_text = "1 job" if len(jobs) == 1 else f"{len(jobs)} jobs"
        cards += f"""
          <a class="item-card" href="/plans/{doc['_id']}">
            <div class="item-card-content">
              <div class="item-title">
                <img class="item-title-icon"
                  src="{escape(item_icon_url(first_job_type_id))}"
                  alt="" onerror="this.style.visibility='hidden'">
                {escape(_name(first_job_type_id))}
              </div>
              <div class="item-line"><span>Jobs</span>
                <span class="item-value">{jobs_text}</span></div>
              <div class="item-line"><span>Created</span>
                <span class="item-value">
                  {_format_timestamp(cast(datetime, doc["created_at"]))}</span></div>
            </div>
          </a>
        """

    body = f"""<div class="page">
      <h1>Plans</h1>
      <div class="item-grid">{cards}</div>
    </div>"""
    return HTMLResponse(render_page("Plans", body, _LIST_STYLE, character=character))


@router.get("/create")
async def create_plan_from_build(
    character: CharacterDocument = Depends(get_current_character),
    db: AsyncIOMotorDatabase = Depends(get_database),
    type_id: int = Query(...),
    qty: int = Query(default=1, ge=1),
    build: str = Query(default=""),
) -> RedirectResponse:
    build_set = frozenset(int(t) for t in build.split(",") if t.strip().isdigit())
    plan_id = await plan.create_plan(db, character.character_id, type_id, qty, build_set)
    return RedirectResponse(f"/plans/{plan_id}")


@router.get("/{plan_id}/add-job")
async def add_job_to_plan(
    plan_id: str,
    character: CharacterDocument = Depends(get_current_character),
    db: AsyncIOMotorDatabase = Depends(get_database),
    type_id: int = Query(...),
    qty: int = Query(default=1, ge=1),
    build: str = Query(default=""),
) -> RedirectResponse:
    if not _PLAN_ID_RE.fullmatch(plan_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid plan id")
    build_set = frozenset(int(t) for t in build.split(",") if t.strip().isdigit())
    job_id = await plan.add_job(db, plan_id, character.character_id, type_id, qty, build_set)
    if job_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan not found")
    return RedirectResponse(f"/plans/{plan_id}")


@router.get("/{plan_id}", response_class=HTMLResponse)
async def plan_detail(
    plan_id: str,
    character: CharacterDocument = Depends(get_current_character),
    db: AsyncIOMotorDatabase = Depends(get_database),
    redis: Redis | None = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    doc = await plan.get_plan(db, plan_id, character.character_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan not found")

    jobs = cast(list[dict[str, object]], doc["jobs"])
    resolutions = [
        await build_chain.resolve_build_chain(
            db,
            redis,
            settings,
            cast(int, job["target_type_id"]),
            cast(int, job["target_quantity"]),
            frozenset(cast(list[int], job["build_set"])),
        )
        for job in jobs
    ]

    page_title = "Plan - eve-build"

    add_to_plan_href = escape(f"/build/items?plan_id={plan_id}")
    header = f"""
      <div class="header">
        <div>
          <div class="name">Plan</div>
          <div class="meta">saved {_format_timestamp(cast(datetime, doc["created_at"]))}</div>
        </div>
      </div>
      <a class="btn btn-primary" href="{add_to_plan_href}">Add to Plan</a>
    """

    total_cost = sum(resolution.raw_material_cost for resolution in resolutions)
    total_value = sum(resolution.output_value for resolution in resolutions)
    stats = (
        summary_stat_html(format_isk(total_cost), "Total raw material cost")
        + summary_stat_html(format_isk(total_value), "Total output value")
        + summary_stat_html(format_isk(total_value - total_cost), "Total profit")
        + summary_stat_html(str(len(resolutions)), "Jobs")
    )

    job_cards = ""
    for resolution in resolutions:
        job_profit = resolution.output_value - resolution.raw_material_cost
        job_cards += f"""
          <div class="item-card">
            <div class="item-card-content">
              <div class="item-title">
                <img class="item-title-icon"
                  src="{escape(item_icon_url(resolution.target_type_id))}"
                  alt="" onerror="this.style.visibility='hidden'">
                {escape(resolution.target_name)} &times;{resolution.target_quantity}
              </div>
              <div class="item-line"><span>Cost</span>
                <span class="item-value">{format_isk(resolution.raw_material_cost)}</span></div>
              <div class="item-line"><span>Profit</span>
                <span class="item-value">{format_isk(job_profit)}</span></div>
              {_materials_table(resolution.raw_materials)}
            </div>
          </div>
        """
    jobs_section = section_html("Jobs", job_cards)

    combined_materials = build_chain.aggregate_raw_materials(resolutions)
    materials_section = section_html("Materials Needed", _materials_table(combined_materials))

    body = f"""<div class="page">{header}
      <div class="summary">{stats}</div>
      {jobs_section}
      {materials_section}
      <a class="btn btn-secondary back" href="/plans">Back to plans</a>
    </div>"""
    return HTMLResponse(render_page(page_title, body, _DETAIL_STYLE, character=character))
