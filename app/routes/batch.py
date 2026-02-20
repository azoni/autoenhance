"""
Core deliverable: batch download all enhanced images for an order as a ZIP.

GET /orders/{order_id}/images
"""

import asyncio
import logging
import os
import tempfile
import zipfile
from typing import Literal, Optional

import httpx
from fastapi import APIRouter, Query
from fastapi.exceptions import HTTPException
from fastapi.responses import StreamingResponse

from app.config import (
    API_BASE,
    EXT_MAP,
    MAX_CONCURRENT_DOWNLOADS,
    MAX_IMAGES_PER_ORDER,
    UUID_RE,
)
from app.state import stats, get_api_key, get_http_client

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/orders/{order_id}/images")
async def batch_download_order_images(
    order_id: str,
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
):
    """
    Download all enhanced images for an order as a ZIP archive.

    **Workflow:**
    1. Retrieves the order from the Autoenhance API.
    2. Downloads each enhanced image concurrently (up to 5 at a time).
    3. Bundles all successfully downloaded images into a ZIP file.

    **Partial failure handling:**
    If some images fail (e.g. still processing), the ZIP will include a
    `_download_report.txt` listing each failed image ID and the reason.
    Response headers `X-Downloaded` and `X-Failed` indicate the counts.

    To recover failed images, either:
    - **Retry the batch** — already-processed images download instantly.
    - **Fetch individually** via `GET /v3/images/{image_id}/enhanced`
      using the image IDs from the download report.

    If *all* images fail, a 422 error is returned instead of a ZIP.
    """
    # Validate order_id is a UUID before making upstream calls
    if not UUID_RE.match(order_id):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid order ID format. Expected a UUID, got: '{order_id}'",
        )

    api_key = get_api_key()
    headers = {"x-api-key": api_key}
    if dev_mode:
        headers["x-dev-mode"] = "true"

    client = get_http_client()

    # ---- Step 1: Retrieve the order ----
    logger.info("Retrieving order %s", order_id)
    try:
        order_resp = await client.get(
            f"{API_BASE}/orders/{order_id}", headers=headers
        )
    except httpx.HTTPError as exc:
        logger.error("Network error retrieving order %s: %s", order_id, exc)
        raise HTTPException(
            status_code=502,
            detail="Failed to reach Autoenhance API.",
        )

    if order_resp.status_code == 404:
        raise HTTPException(
            status_code=404, detail=f"Order '{order_id}' not found."
        )
    if order_resp.status_code == 401:
        raise HTTPException(
            status_code=401, detail="Invalid or missing API key."
        )
    if order_resp.status_code != 200:
        logger.error("Upstream error retrieving order %s: %d %s", order_id, order_resp.status_code, order_resp.text)
        raise HTTPException(
            status_code=502,
            detail=f"Failed to retrieve order (upstream returned {order_resp.status_code}).",
        )

    order = order_resp.json()
    images = order.get("images", [])
    order_name = order.get("name", order_id)

    if not images:
        raise HTTPException(
            status_code=404,
            detail=f"Order '{order_name}' contains no images.",
        )

    if len(images) > MAX_IMAGES_PER_ORDER:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Order contains {len(images)} images, exceeding the "
                f"limit of {MAX_IMAGES_PER_ORDER}. Use the single-image "
                f"endpoint for large orders."
            ),
        )

    logger.info(
        "Order '%s' has %d image(s) — starting downloads", order_name, len(images)
    )

    # ---- Step 2: Download each enhanced image concurrently ----
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

    async def download_image(image: dict) -> dict:
        image_id = image.get("image_id") or image.get("id")
        image_name = (
            image.get("image_name")
            or image.get("name")
            or f"image_{image_id}"
        )

        params: dict = {}
        if not preview:
            params["preview"] = "false"
        params["format"] = image_format
        if quality is not None:
            params["quality"] = quality

        async with semaphore:
            last_error = None
            for attempt in range(2):  # 1 automatic retry on transient failure
                try:
                    resp = await client.get(
                        f"{API_BASE}/images/{image_id}/enhanced",
                        headers=headers,
                        params=params,
                    )
                except httpx.TimeoutException:
                    last_error = "Download timed out"
                    if attempt == 0:
                        logger.warning("Timeout downloading %s, retrying…", image_id)
                        await asyncio.sleep(1)
                        continue
                    logger.error("Timeout downloading image %s (after retry)", image_id)
                    break

                if resp.status_code == 200:
                    logger.info("Downloaded image %s (%s)", image_id, image_name)
                    return {
                        "image_id": image_id,
                        "name": image_name,
                        "content": resp.content,
                        "error": None,
                    }

                last_error = f"HTTP {resp.status_code}"
                if resp.status_code < 500:
                    break  # don't retry client errors (4xx)
                if attempt == 0:
                    logger.warning("Server error %d for %s, retrying…", resp.status_code, image_id)
                    await asyncio.sleep(1)

            logger.warning("Failed to download image %s: %s", image_id, last_error)
            return {
                "image_id": image_id,
                "name": image_name,
                "content": None,
                "error": last_error,
            }

    # ---- Step 3: Stream results into ZIP as downloads complete ----
    # Using as_completed so each image is written to the ZIP and freed
    # immediately.  Peak memory is bounded by the semaphore (max 5
    # in-flight downloads) rather than the total order size.
    ext = EXT_MAP.get(image_format, image_format)

    zip_buffer = tempfile.SpooledTemporaryFile(max_size=10 * 1024 * 1024)
    downloaded = 0
    failed: list[dict] = []
    seen: set[str] = set()

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_STORED) as zf:
        for coro in asyncio.as_completed(
            [download_image(img) for img in images]
        ):
            result = await coro

            if result["content"] is None:
                failed.append(result)
                continue

            base = os.path.splitext(result["name"])[0]
            unique = base
            counter = 1
            while unique in seen:
                unique = f"{base}_{counter}"
                counter += 1
            seen.add(unique)
            zf.writestr(f"{unique}.{ext}", result["content"])
            del result["content"]  # free immediately
            downloaded += 1

        if not downloaded:
            await stats.record_batch_total_failure(
                failed=len(failed), order_id=order_id,
            )
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "No images could be downloaded. They may still be processing.",
                    "failures": [
                        {"image_id": f["image_id"], "error": f["error"]}
                        for f in failed
                    ],
                },
            )

        if failed:
            report_lines = [
                f"Download report for order: {order_name}",
                f"Downloaded: {downloaded}/{len(images)}",
                "",
                "Failed:",
            ]
            for f in failed:
                report_lines.append(
                    f"  - {f['image_id']} ({f['name']}): {f['error']}"
                )
            report_lines += [
                "",
                "To recover these images:",
                "  1. Retry the batch endpoint — already-processed images download instantly.",
                "  2. Or fetch individually: GET /v3/images/{image_id}/enhanced",
            ]
            zf.writestr("_download_report.txt", "\n".join(report_lines))

    zip_buffer.seek(0)

    # Sanitise the order name for use as a filename
    safe_name = "".join(
        c if c.isalnum() or c in "-_ " else "_" for c in order_name
    )

    await stats.record_batch_complete(
        downloaded=downloaded, failed=len(failed),
        order_id=order_id, total=len(images),
    )

    logger.info(
        "Returning ZIP: %d downloaded, %d failed", downloaded, len(failed)
    )

    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}_images.zip"',
            "X-Total-Images": str(len(images)),
            "X-Downloaded": str(downloaded),
            "X-Failed": str(len(failed)),
        },
    )
