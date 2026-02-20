"""
Order creation endpoints for testing the batch download flow.

POST /api/create-order
POST /api/create-sample-order
"""

import asyncio
import logging
import os

import httpx
from fastapi import APIRouter, File, Request, UploadFile
from fastapi.exceptions import HTTPException

from app.auth import require_admin
from app.config import (
    API_BASE,
    CONTENT_TYPE_MAP,
    MAX_UPLOAD_SIZE_BYTES,
    SAMPLE_IMAGES_DIR,
)
from app.state import stats, get_api_key, get_http_client

logger = logging.getLogger(__name__)

router = APIRouter()


async def _create_order(
    order_name: str,
    images: list[tuple[str, bytes, str]],
) -> dict:
    """Shared helper: create an Autoenhance order and upload images.

    Args:
        order_name: Display name for the order.
        images: List of (image_name, content_bytes, content_type) tuples.

    Returns:
        Dict with order_id, images_uploaded count, and image details.
    """
    api_key = get_api_key()
    headers = {"x-api-key": api_key, "Content-Type": "application/json"}
    client = get_http_client()

    order_resp = await client.post(
        f"{API_BASE}/orders",
        headers=headers,
        json={"name": order_name},
    )
    if order_resp.status_code not in (200, 201):
        logger.error("Failed to create order: upstream %d %s", order_resp.status_code, order_resp.text)
        raise HTTPException(
            status_code=502,
            detail="Failed to create order upstream.",
        )

    order_data = order_resp.json()
    order_id = order_data["order_id"]
    logger.info("Created order %s", order_id)

    async def _upload_one(image_name: str, content: bytes, content_type: str) -> dict | None:
        try:
            reg_resp = await client.post(
                f"{API_BASE}/images/",
                headers=headers,
                json={
                    "image_name": image_name,
                    "order_id": order_id,
                    "contentType": content_type,
                },
            )
            if reg_resp.status_code not in (200, 201):
                logger.warning("Failed to register %s: %s", image_name, reg_resp.text)
                return None

            reg_data = reg_resp.json()
            upload_url = reg_data.get("s3PutObjectUrl") or reg_data.get("upload_url")
            image_id = reg_data.get("image_id")

            if not upload_url:
                logger.warning("No upload URL returned for %s", image_name)
                return None

            put_resp = await client.put(
                upload_url,
                content=content,
                headers={"Content-Type": content_type},
            )
            if put_resp.status_code in (200, 201):
                logger.info("Uploaded %s (%s)", image_name, image_id)
                return {"image_id": image_id, "name": image_name}

            logger.warning("S3 upload failed for %s: %d", image_name, put_resp.status_code)
            return None
        except httpx.HTTPError as exc:
            logger.warning("Network error uploading %s: %s", image_name, exc)
            return None

    results = await asyncio.gather(*[_upload_one(n, c, ct) for n, c, ct in images])
    uploaded = [r for r in results if r is not None]

    await stats.record_order_created(images_uploaded=len(uploaded))

    return {
        "order_id": order_id,
        "images_uploaded": len(uploaded),
        "images": uploaded,
    }


@router.post("/api/create-order", include_in_schema=False)
async def create_test_order(request: Request, files: list[UploadFile] = File(...)):
    """Upload images to Autoenhance and create a new order for testing."""
    require_admin(request)
    if not files:
        raise HTTPException(status_code=400, detail="No files provided.")

    images = []
    for file in files:
        # Check declared size before reading into memory when available
        if file.size is not None and file.size > MAX_UPLOAD_SIZE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"File '{file.filename}' exceeds the 20 MB limit.",
            )
        content = await file.read()
        if len(content) > MAX_UPLOAD_SIZE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"File '{file.filename}' exceeds the 20 MB limit.",
            )
        ext = os.path.splitext(file.filename or "image.jpg")[1].lower()
        content_type = CONTENT_TYPE_MAP.get(ext, "image/jpeg")
        image_name = os.path.splitext(file.filename or "image")[0]
        images.append((image_name, content, content_type))

    return await _create_order(f"Test Order ({len(images)} images)", images)


@router.post("/api/create-sample-order", include_in_schema=False)
async def create_sample_order(request: Request):
    """Create a test order using the bundled sample images — no upload needed."""
    require_admin(request)
    if not SAMPLE_IMAGES_DIR.exists():
        raise HTTPException(status_code=500, detail="Sample images directory not found.")

    sample_files = sorted(
        p for p in SAMPLE_IMAGES_DIR.iterdir()
        if p.suffix.lower() in CONTENT_TYPE_MAP
    )
    if not sample_files:
        raise HTTPException(status_code=500, detail="No sample images found.")

    images = []
    for path in sample_files:
        content = await asyncio.to_thread(path.read_bytes)
        content_type = CONTENT_TYPE_MAP[path.suffix.lower()]
        images.append((path.stem, content, content_type))

    return await _create_order(f"Sample Order ({len(images)} images)", images)
