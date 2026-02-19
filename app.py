"""
Autoenhance Batch Image Downloader

A FastAPI service providing a batch endpoint to download all enhanced images
for a given Autoenhance order as a ZIP archive.

Endpoints:
    GET /orders/{order_id}/images  - Download all images for an order as a ZIP
    GET /health                    - Health check
"""

import asyncio
import io
import logging
import os
import zipfile
from typing import Literal, Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

API_BASE = "https://api.autoenhance.ai/v3"

# Limit concurrent downloads to avoid overwhelming the API
MAX_CONCURRENT_DOWNLOADS = 5

app = FastAPI(
    title="Autoenhance Batch Image Downloader",
    description=(
        "Batch endpoint that retrieves all enhanced images for a given order "
        "and returns them as a ZIP archive."
    ),
    version="1.0.0",
)


def _get_api_key() -> str:
    key = os.getenv("AUTOENHANCE_API_KEY")
    if not key:
        raise HTTPException(
            status_code=500,
            detail="AUTOENHANCE_API_KEY environment variable is not set.",
        )
    return key


@app.get("/orders/{order_id}/images")
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

    If some images fail (e.g. still processing), the ZIP will include a
    `_download_report.txt` with details. If *all* images fail, a 422 error
    is returned.
    """
    api_key = _get_api_key()
    headers = {"x-api-key": api_key}
    if dev_mode:
        headers["x-dev-mode"] = "true"

    async with httpx.AsyncClient(timeout=60.0) as client:
        # ---- Step 1: Retrieve the order ----
        logger.info("Retrieving order %s", order_id)
        order_resp = await client.get(
            f"{API_BASE}/orders/{order_id}", headers=headers
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
            raise HTTPException(
                status_code=order_resp.status_code,
                detail=f"Failed to retrieve order: {order_resp.text}",
            )

        order = order_resp.json()
        images = order.get("images", [])
        order_name = order.get("name", order_id)

        if not images:
            raise HTTPException(
                status_code=404,
                detail=f"Order '{order_name}' contains no images.",
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
                try:
                    resp = await client.get(
                        f"{API_BASE}/images/{image_id}/enhanced",
                        headers=headers,
                        params=params,
                    )
                except httpx.TimeoutException:
                    logger.error("Timeout downloading image %s", image_id)
                    return {
                        "image_id": image_id,
                        "name": image_name,
                        "content": None,
                        "error": "Download timed out",
                    }

                if resp.status_code == 200:
                    logger.info("Downloaded image %s (%s)", image_id, image_name)
                    return {
                        "image_id": image_id,
                        "name": image_name,
                        "content": resp.content,
                        "error": None,
                    }

                logger.warning(
                    "Failed to download image %s: HTTP %d", image_id, resp.status_code
                )
                return {
                    "image_id": image_id,
                    "name": image_name,
                    "content": None,
                    "error": f"HTTP {resp.status_code}",
                }

        results = await asyncio.gather(*[download_image(img) for img in images])

        # ---- Step 3: Bundle into a ZIP archive ----
        successful = [r for r in results if r["content"] is not None]
        failed = [r for r in results if r["content"] is None]

        if not successful:
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

        ext_map = {
            "jpeg": "jpg",
            "png": "png",
            "webp": "webp",
            "avif": "avif",
            "jxl": "jxl",
        }
        ext = ext_map.get(image_format, image_format)

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            seen: set[str] = set()
            for result in successful:
                # Strip any existing extension and add the requested one
                base = os.path.splitext(result["name"])[0]
                unique = base
                counter = 1
                while unique in seen:
                    unique = f"{base}_{counter}"
                    counter += 1
                seen.add(unique)
                zf.writestr(f"{unique}.{ext}", result["content"])

            if failed:
                report_lines = [
                    f"Download report for order: {order_name}",
                    f"Downloaded: {len(successful)}/{len(images)}",
                    "",
                    "Failed:",
                ]
                for f in failed:
                    report_lines.append(
                        f"  - {f['image_id']} ({f['name']}): {f['error']}"
                    )
                zf.writestr("_download_report.txt", "\n".join(report_lines))

        zip_buffer.seek(0)

        # Sanitise the order name for use as a filename
        safe_name = "".join(
            c if c.isalnum() or c in "-_ " else "_" for c in order_name
        )

        logger.info(
            "Returning ZIP: %d downloaded, %d failed", len(successful), len(failed)
        )

        return StreamingResponse(
            zip_buffer,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{safe_name}_images.zip"',
                "X-Total-Images": str(len(images)),
                "X-Downloaded": str(len(successful)),
                "X-Failed": str(len(failed)),
            },
        )


@app.get("/health")
async def health_check():
    """Health check — also indicates whether the API key is configured."""
    return {
        "status": "ok",
        "api_key_configured": bool(os.getenv("AUTOENHANCE_API_KEY")),
    }
