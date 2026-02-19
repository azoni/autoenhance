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
import re
import zipfile
from typing import Literal, Optional

import httpx
import sentry_sdk
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pathlib import Path

load_dotenv(Path(__file__).resolve().parent / ".env")

# Sentry error tracking — only active when DSN is configured
if os.getenv("SENTRY_DSN"):
    sentry_sdk.init(
        dsn=os.getenv("SENTRY_DSN"),
        traces_sample_rate=0.2,
        environment=os.getenv("SENTRY_ENV", "production"),
    )

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

API_BASE = "https://api.autoenhance.ai/v3"

# Limit concurrent downloads to avoid overwhelming the API
MAX_CONCURRENT_DOWNLOADS = 5

# Pre-compiled UUID pattern — validated once at import, not per-request
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

CONTENT_TYPE_MAP = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

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
    # Validate order_id is a UUID before making upstream calls
    if not _UUID_RE.match(order_id):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid order ID format. Expected a UUID, got: '{order_id}'",
        )

    api_key = _get_api_key()
    headers = {"x-api-key": api_key}
    if dev_mode:
        headers["x-dev-mode"] = "true"

    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
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


SAMPLE_IMAGES_DIR = Path(__file__).resolve().parent / "sample_images"


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
    api_key = _get_api_key()
    headers = {"x-api-key": api_key, "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        order_resp = await client.post(
            f"{API_BASE}/orders",
            headers=headers,
            json={"name": order_name},
        )
        if order_resp.status_code not in (200, 201):
            raise HTTPException(
                status_code=order_resp.status_code,
                detail=f"Failed to create order: {order_resp.text}",
            )

        order_data = order_resp.json()
        order_id = order_data["order_id"]
        logger.info("Created order %s", order_id)

        uploaded = []
        for image_name, content, content_type in images:
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
                continue

            reg_data = reg_resp.json()
            upload_url = reg_data.get("s3PutObjectUrl") or reg_data.get("upload_url")
            image_id = reg_data.get("image_id")

            if not upload_url:
                logger.warning("No upload URL returned for %s", image_name)
                continue

            put_resp = await client.put(
                upload_url,
                content=content,
                headers={"Content-Type": content_type},
            )
            if put_resp.status_code in (200, 201):
                uploaded.append({"image_id": image_id, "name": image_name})
                logger.info("Uploaded %s (%s)", image_name, image_id)
            else:
                logger.warning("S3 upload failed for %s: %d", image_name, put_resp.status_code)

    return {
        "order_id": order_id,
        "images_uploaded": len(uploaded),
        "images": uploaded,
    }


@app.post("/api/create-order", include_in_schema=False)
async def create_test_order(files: list[UploadFile] = File(...)):
    """Upload images to Autoenhance and create a new order for testing."""
    if not files:
        raise HTTPException(status_code=400, detail="No files provided.")

    images = []
    for file in files:
        content = await file.read()
        ext = os.path.splitext(file.filename or "image.jpg")[1].lower()
        content_type = CONTENT_TYPE_MAP.get(ext, "image/jpeg")
        image_name = os.path.splitext(file.filename or "image")[0]
        images.append((image_name, content, content_type))

    return await _create_order(f"Test Order ({len(images)} images)", images)


@app.post("/api/create-sample-order", include_in_schema=False)
async def create_sample_order():
    """Create a test order using the bundled sample images — no upload needed."""
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


@app.get("/", response_class=HTMLResponse)
async def ui():
    """Web UI for testing the batch download endpoint — served from static/index.html."""
    html_path = Path(__file__).resolve().parent / "static" / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(Path(__file__).resolve().parent / "favicon.ico", media_type="image/x-icon")


@app.api_route("/health", methods=["GET", "HEAD"])
async def health_check():
    """Health check — also indicates whether the API key is configured."""
    return {
        "status": "ok",
        "api_key_configured": bool(os.getenv("AUTOENHANCE_API_KEY")),
    }


@app.get("/sentry-debug", include_in_schema=False)
async def trigger_error():
    """Trigger a test error to verify Sentry integration."""
    division_by_zero = 1 / 0


@app.get("/api/sentry/issues", include_in_schema=False)
async def sentry_issues():
    """Proxy to Sentry API — returns recent issues for the project."""
    token = os.getenv("SENTRY_AUTH_TOKEN")
    org = os.getenv("SENTRY_ORG")
    project = os.getenv("SENTRY_PROJECT")
    if not all([token, org, project]):
        return {"issues": [], "error": "Sentry API not configured"}

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(
                f"https://sentry.io/api/0/projects/{org}/{project}/issues/",
                headers={"Authorization": f"Bearer {token}"},
                params={"query": "", "limit": 10, "sort": "date"},
            )
            if resp.status_code != 200:
                return {"issues": [], "error": f"Sentry API returned {resp.status_code}"}
            issues = resp.json()
            return {
                "issues": [
                    {
                        "id": i.get("id"),
                        "title": i.get("title"),
                        "culprit": i.get("culprit"),
                        "count": i.get("count"),
                        "firstSeen": i.get("firstSeen"),
                        "lastSeen": i.get("lastSeen"),
                        "level": i.get("level"),
                        "status": i.get("status"),
                        "permalink": i.get("permalink"),
                    }
                    for i in issues
                ]
            }
        except Exception as e:
            return {"issues": [], "error": str(e)}
