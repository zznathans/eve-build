from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from motor.motor_asyncio import AsyncIOMotorDatabase
from redis.asyncio import Redis

from app.core.config import Settings, get_settings
from app.db.mongo import get_database
from app.db.redis import get_redis
from app.deps import get_current_character_optional
from app.models.character import CharacterDocument
from app.services import character_data, esi, industry, locations, sde
from app.services.locations import LocationInfo
from app.templating import templates
from app.web import gauge_cell_html, humanize_relative_time, icon_url, location_label_html

router = APIRouter()

_LOGIN_STYLE = ["/static/health-login.css"]
_DASHBOARD_STYLE = ["/static/health-dashboard.css"]


def _job_row(
    job: esi.IndustryJobEntry,
    blueprint_type_docs: dict[int, dict[str, object]],
    job_location_info: dict[int, LocationInfo],
) -> dict[str, object]:
    blueprint_name = str(
        blueprint_type_docs.get(job.blueprint_type_id, {}).get(
            "name", f"Type {job.blueprint_type_id}"
        )
    )
    return {
        "job_id": job.job_id,
        "blueprint_name": blueprint_name,
        "icon_url": icon_url(job.blueprint_type_id),
        "activity_name": industry.ACTIVITY_NAMES.get(
            job.activity_id, f"Activity {job.activity_id}"
        ),
        "location_label": location_label_html(
            job.facility_id, job_location_info.get(job.facility_id)
        ),
        "runs": job.runs,
        "progress_gauge": gauge_cell_html(industry.job_progress_percentage(job)),
        "end_date": job.end_date,
        "ends_in": humanize_relative_time(datetime.fromisoformat(job.end_date)),
    }


@router.get("/", response_class=HTMLResponse)
async def read_root(
    request: Request,
    character: CharacterDocument | None = Depends(get_current_character_optional),
    db: AsyncIOMotorDatabase = Depends(get_database),
    redis: Redis | None = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    if character is None:
        return templates.TemplateResponse(
            request, "health/login.html", {"character": None, "extra_stylesheets": _LOGIN_STYLE}
        )

    blueprints, _ = await character_data.get_merged_blueprints(db, redis, settings, character)
    assets, _ = await character_data.get_merged_assets(db, redis, settings, character)
    jobs, _ = await character_data.get_merged_industry_jobs(db, redis, settings, character)
    active_jobs = [job for job in jobs if job.status == "active"]
    blueprint_type_docs = await sde.type_docs(
        db, redis, settings, {job.blueprint_type_id for job in active_jobs}
    )
    job_location_info = await locations.resolve_location_info(
        db, redis, settings, character.access_token, {job.facility_id for job in active_jobs}
    )

    return templates.TemplateResponse(
        request,
        "health/dashboard.html",
        {
            "character": character,
            "extra_stylesheets": _DASHBOARD_STYLE,
            "blueprint_count": len(blueprints),
            "asset_count": len(assets),
            "active_job_count": len(active_jobs),
            "job_rows": [
                _job_row(job, blueprint_type_docs, job_location_info) for job in active_jobs
            ],
        },
    )


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
