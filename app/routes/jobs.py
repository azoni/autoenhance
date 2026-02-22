"""
Async job pattern for large batch downloads.

POST /orders/{order_id}/jobs  — Start a job, returns job_id immediately (202)
GET  /jobs/{job_id}           — Poll job status
GET  /jobs/{job_id}/download  — Download the ZIP when complete

Use this instead of GET /orders/{order_id}/images for large orders to avoid
hitting server-side response timeouts (e.g. Render's 30s limit on free tier).
"""

import io
import logging
import uuid
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from fastapi.exceptions import HTTPException
from fastapi.responses import StreamingResponse

from app.auth import require_service_key
from app.config import UUID_RE
from app.state import (
    get_cached_zip,
    get_job,
    set_cached_zip,
    set_job,
    update_job,
)
from app.routes.batch import _run_batch

logger = logging.getLogger(__name__)

router = APIRouter()


async def _process_job(
    job_id: str,
    order_id: str,
    image_format: str,
    quality: Optional[int],
    preview: bool,
    dev_mode: bool,
) -> None:
    """Background task: run batch download and store result in the job store."""
    try:
        cache_key = (order_id, image_format, quality, preview, dev_mode)
        cached = get_cached_zip(cache_key)
        if cached:
            logger.info("Job %s: cache hit for order %s", job_id, order_id)
            update_job(job_id, {
                "status": "complete",
                "zip_bytes": cached["zip_bytes"],
                "filename": cached["filename"],
                "headers": cached["headers"],
            })
            return

        zip_bytes, filename, response_headers = await _run_batch(
            order_id, image_format, quality, preview, dev_mode
        )
        set_cached_zip(cache_key, zip_bytes, filename, response_headers)
        update_job(job_id, {
            "status": "complete",
            "zip_bytes": zip_bytes,
            "filename": filename,
            "headers": response_headers,
        })
        logger.info("Job %s complete", job_id)

    except HTTPException as exc:
        logger.error("Job %s failed (HTTP %d): %s", job_id, exc.status_code, exc.detail)
        update_job(job_id, {"status": "error", "error": str(exc.detail)})
    except Exception as exc:
        logger.error("Job %s failed unexpectedly: %s", job_id, exc)
        update_job(job_id, {"status": "error", "error": "Unexpected error. Please try again."})


@router.post("/orders/{order_id}/jobs", status_code=202)
async def create_batch_job(
    order_id: str,
    background_tasks: BackgroundTasks,
    preview: bool = Query(
        True,
        description=(
            "If true, downloads free preview-quality images. "
            "Set to false for full-quality downloads (consumes credits)."
        ),
    ),
    image_format: Literal["jpeg", "png", "webp", "avif", "jxl"] = Query(
        "jpeg",
        alias="format",
        description="Output image format.",
    ),
    quality: Optional[int] = Query(
        None,
        description="Image quality (1-90). Leave blank for API default.",
        ge=1,
        le=90,
    ),
    dev_mode: bool = Query(
        False,
        description=(
            "Enable development mode to test without consuming credits. "
            "Output images will have a watermark."
        ),
    ),
    _: None = Depends(require_service_key),
):
    """
    Start an async batch download job. Returns a job ID immediately (202).

    **Workflow:**
    1. `POST /orders/{order_id}/jobs` → `{"job_id": "..."}` (202)
    2. `GET /jobs/{job_id}` → poll until `status` is `"complete"` or `"error"`
    3. `GET /jobs/{job_id}/download` → download the ZIP

    Jobs expire after 1 hour. Use this for large orders to avoid timeouts.
    """
    if not UUID_RE.match(order_id):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid order ID format. Expected a UUID, got: '{order_id}'",
        )

    job_id = str(uuid.uuid4())
    set_job(job_id, {
        "status": "processing",
        "zip_bytes": None,
        "filename": None,
        "headers": None,
        "error": None,
    })

    background_tasks.add_task(
        _process_job, job_id, order_id, image_format, quality, preview, dev_mode
    )

    logger.info("Job %s created for order %s", job_id, order_id)
    return {"job_id": job_id}


@router.get("/jobs/{job_id}")
async def get_job_status(
    job_id: str,
    _: None = Depends(require_service_key),
):
    """Poll the status of an async batch job."""
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found or expired.")
    return {
        "job_id": job_id,
        "status": job["status"],
        "error": job.get("error"),
    }


@router.get("/jobs/{job_id}/download")
async def download_job_result(
    job_id: str,
    _: None = Depends(require_service_key),
):
    """Download the ZIP archive for a completed async batch job."""
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found or expired.")
    if job["status"] == "processing":
        raise HTTPException(
            status_code=409,
            detail="Job is still processing. Poll GET /jobs/{job_id} for status.",
        )
    if job["status"] == "error":
        raise HTTPException(status_code=422, detail=job.get("error", "Job failed."))

    return StreamingResponse(
        io.BytesIO(job["zip_bytes"]),
        media_type="application/zip",
        headers=job["headers"],
    )
