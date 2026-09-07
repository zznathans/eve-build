from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from motor.motor_asyncio import AsyncIOMotorDatabase
from redis.asyncio import Redis

from app.core.config import Settings, get_settings
from app.db.mongo import get_database
from app.db.redis import get_redis
from app.deps import get_current_character
from app.models.character import CharacterDocument
from app.services import character_data, industry, locations, sde
from app.templating import templates
from app.web import gauge_cell_html, icon_url, location_label_html

router = APIRouter(prefix="/jobs", tags=["jobs"])

_DETAIL_STYLE = ["/static/jobs-detail.css"]


@router.get("/{job_id}", response_class=HTMLResponse)
async def job_detail(
    request: Request,
    job_id: int,
    character: CharacterDocument = Depends(get_current_character),
    db: AsyncIOMotorDatabase = Depends(get_database),
    redis: Redis | None = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    jobs, _ = await character_data.get_merged_industry_jobs(db, redis, settings, character)
    job = next((j for j in jobs if j.job_id == job_id), None)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")

    type_ids = {job.blueprint_type_id}
    if job.product_type_id is not None:
        type_ids.add(job.product_type_id)
    type_docs = await sde.type_docs(db, redis, settings, type_ids)
    location_info = await locations.resolve_location_info(
        db, redis, settings, character.access_token, {job.facility_id}
    )

    blueprint_name = str(
        type_docs.get(job.blueprint_type_id, {}).get("name", f"Type {job.blueprint_type_id}")
    )
    product_name = None
    if job.product_type_id is not None:
        product_name = str(
            type_docs.get(job.product_type_id, {}).get("name", f"Type {job.product_type_id}")
        )

    return templates.TemplateResponse(
        request,
        "jobs/detail.html",
        {
            "character": character,
            "extra_stylesheets": _DETAIL_STYLE,
            "job": job,
            "blueprint_name": blueprint_name,
            "product_name": product_name,
            "job_icon_url": icon_url(job.blueprint_type_id),
            "activity_name": industry.ACTIVITY_NAMES.get(
                job.activity_id, f"Activity {job.activity_id}"
            ),
            "status_label": job.status.capitalize(),
            "location_label": location_label_html(
                job.facility_id, location_info.get(job.facility_id)
            ),
            "progress_gauge": gauge_cell_html(industry.job_progress_percentage(job)),
        },
    )
