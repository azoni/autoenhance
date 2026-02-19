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
from typing import List, Literal, Optional

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
    uuid_pattern = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
    )
    if not uuid_pattern.match(order_id):
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


@app.post("/api/create-order", include_in_schema=False)
async def create_test_order(files: List[UploadFile] = File(...)):
    """Upload images to Autoenhance and create a new order for testing."""
    if not files:
        raise HTTPException(status_code=400, detail="No files provided.")

    api_key = _get_api_key()
    headers = {"x-api-key": api_key, "Content-Type": "application/json"}

    content_type_map = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }

    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        # 1. Create order
        order_resp = await client.post(
            f"{API_BASE}/orders",
            headers=headers,
            json={"name": f"Test Order ({len(files)} images)"},
        )
        if order_resp.status_code not in (200, 201):
            raise HTTPException(
                status_code=order_resp.status_code,
                detail=f"Failed to create order: {order_resp.text}",
            )

        order_data = order_resp.json()
        order_id = order_data["order_id"]
        logger.info("Created order %s", order_id)

        # 2. Register and upload each image
        uploaded = []
        for file in files:
            content = await file.read()
            ext = os.path.splitext(file.filename or "image.jpg")[1].lower()
            content_type = content_type_map.get(ext, "image/jpeg")
            image_name = os.path.splitext(file.filename or "image")[0]

            # Register image with Autoenhance
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

            # Upload binary to presigned S3 URL
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


SAMPLE_IMAGES_DIR = Path(__file__).resolve().parent / "sample_images"

SAMPLE_CONTENT_TYPE_MAP = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


@app.post("/api/create-sample-order", include_in_schema=False)
async def create_sample_order():
    """Create a test order using the bundled sample images — no upload needed."""
    if not SAMPLE_IMAGES_DIR.exists():
        raise HTTPException(status_code=500, detail="Sample images directory not found.")

    sample_files = sorted(
        p for p in SAMPLE_IMAGES_DIR.iterdir()
        if p.suffix.lower() in SAMPLE_CONTENT_TYPE_MAP
    )
    if not sample_files:
        raise HTTPException(status_code=500, detail="No sample images found.")

    api_key = _get_api_key()
    headers = {"x-api-key": api_key, "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        # 1. Create order
        order_resp = await client.post(
            f"{API_BASE}/orders",
            headers=headers,
            json={"name": f"Sample Order ({len(sample_files)} images)"},
        )
        if order_resp.status_code not in (200, 201):
            raise HTTPException(
                status_code=order_resp.status_code,
                detail=f"Failed to create order: {order_resp.text}",
            )

        order_data = order_resp.json()
        order_id = order_data["order_id"]
        logger.info("Created sample order %s", order_id)

        # 2. Register and upload each sample image
        uploaded = []
        for path in sample_files:
            content_type = SAMPLE_CONTENT_TYPE_MAP[path.suffix.lower()]
            image_name = path.stem

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

            with open(path, "rb") as f:
                content = f.read()
            put_resp = await client.put(
                upload_url,
                content=content,
                headers={"Content-Type": content_type},
            )
            if put_resp.status_code in (200, 201):
                uploaded.append({"image_id": image_id, "name": image_name})
                logger.info("Uploaded sample %s (%s)", image_name, image_id)
            else:
                logger.warning("S3 upload failed for %s: %d", image_name, put_resp.status_code)

    return {
        "order_id": order_id,
        "images_uploaded": len(uploaded),
        "images": uploaded,
    }


@app.get("/", response_class=HTMLResponse)
async def ui():
    """Simple web UI for testing the batch download endpoint."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Batch Downloader | Autoenhance.ai</title>
<link rel="icon" href="/favicon.ico" type="image/x-icon">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif; background: #f5f6ff; color: #222173; min-height: 100vh; display: flex; flex-direction: column; align-items: center; }
  .topbar { width: 100%; background: #222173; padding: 16px 32px; display: flex; align-items: center; gap: 10px; }
  .topbar svg { height: 28px; }
  .topbar span { color: #fff; font-size: 1rem; font-weight: 600; letter-spacing: -0.2px; }
  .topbar .badge { background: linear-gradient(135deg, #3bd8be, #77bff6); color: #222173; font-size: 0.7rem; font-weight: 700; padding: 2px 8px; border-radius: 9999px; margin-left: 8px; }
  main { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 40px 20px; width: 100%; }
  .card { background: #ffffff; border-radius: 12px; padding: 40px; width: 100%; max-width: 500px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1), 0 2px 4px -2px rgba(0,0,0,0.06); }
  .info-panel { background: #ffffff; border-radius: 12px; padding: 32px 36px; width: 100%; max-width: 500px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1), 0 2px 4px -2px rgba(0,0,0,0.06); margin-top: 20px; }
  .info-panel h2 { font-size: 1.1rem; font-weight: 700; color: #222173; margin-bottom: 16px; }
  .info-panel h3 { font-size: 0.85rem; font-weight: 600; color: #222173; margin: 18px 0 8px 0; }
  .info-panel h3:first-of-type { margin-top: 0; }
  .info-panel p { font-size: 0.82rem; color: #4f5c65; line-height: 1.55; margin-bottom: 6px; }
  .info-panel .tag { display: inline-block; font-size: 0.68rem; font-weight: 600; padding: 2px 7px; border-radius: 4px; margin-right: 4px; }
  .info-panel .tag.batch { background: #e5f1fb; color: #222173; }
  .info-panel .tag.normal { background: #f0f0f5; color: #4f5c65; }
  .compare { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin: 12px 0; }
  .compare .col { padding: 10px 12px; border-radius: 8px; font-size: 0.78rem; line-height: 1.5; }
  .compare .col.left { background: #f0f0f5; color: #4f5c65; }
  .compare .col.right { background: linear-gradient(135deg, rgba(59,216,190,0.1), rgba(119,191,246,0.1)); color: #222173; }
  .compare .col strong { display: block; font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.3px; margin-bottom: 4px; opacity: 0.7; }
  .handled { list-style: none; padding: 0; }
  .handled li { font-size: 0.8rem; color: #4f5c65; padding: 6px 0; border-bottom: 1px solid #f0f0f5; display: flex; gap: 8px; align-items: baseline; }
  .handled li:last-child { border-bottom: none; }
  .handled .check { color: #3bd8be; font-weight: 700; flex-shrink: 0; }
  .handled .assume { color: #e09f3e; font-weight: 700; flex-shrink: 0; font-size: 0.9rem; }
  .handled .prod { flex-shrink: 0; font-size: 0.65rem; }
  .handled .prod.cur { color: #3bd8be; }
  .handled .prod.next { color: #d0d5dd; }
  details summary { cursor: pointer; font-size: 0.85rem; font-weight: 600; color: #222173; padding: 4px 0; }
  details summary:hover { color: #3bd8be; }
  details[open] summary { margin-bottom: 12px; }
  h1 { font-size: 1.5rem; font-weight: 700; color: #222173; margin-bottom: 4px; }
  .sub { color: #4f5c65; font-size: 0.9rem; margin-bottom: 28px; }
  label { display: block; font-size: 0.78rem; color: #4f5c65; margin-bottom: 6px; font-weight: 500; text-transform: uppercase; letter-spacing: 0.5px; }
  input, select { width: 100%; padding: 10px 14px; border: 1px solid #d0d5dd; border-radius: 8px; background: #fcfcfd; color: #222173; font-size: 0.95rem; margin-bottom: 18px; outline: none; transition: border-color 0.2s, box-shadow 0.2s; }
  input:focus, select:focus { border-color: #3bd8be; box-shadow: 0 0 0 3px rgba(59,216,190,0.15); }
  input::placeholder { color: #a0a8b4; }
  .row { display: flex; gap: 12px; }
  .row > div { flex: 1; }
  .checks { display: flex; gap: 24px; margin-bottom: 24px; }
  .checks label { display: flex; align-items: center; gap: 7px; text-transform: none; font-size: 0.9rem; color: #222173; cursor: pointer; font-weight: 400; }
  .checks input[type="checkbox"] { width: 16px; height: 16px; margin: 0; accent-color: #3bd8be; }
  button { width: 100%; padding: 12px; border: none; border-radius: 8px; background: linear-gradient(135deg, #28dbbf, #77bff6); color: #222173; font-size: 1rem; font-weight: 700; cursor: pointer; transition: opacity 0.2s, transform 0.1s; }
  button:hover { opacity: 0.9; transform: translateY(-1px); }
  button:active { transform: translateY(0); }
  button:disabled { background: #d0d5dd; color: #a0a8b4; cursor: not-allowed; transform: none; }
  #status { margin-top: 16px; padding: 12px 14px; border-radius: 8px; font-size: 0.85rem; display: none; }
  #status.info { display: block; background: #e5f1fb; border: 1px solid #b8d4ec; color: #4f5c65; }
  #status.ok { display: block; background: #ecfdf5; border: 1px solid #a7f3d0; color: #166534; }
  #status.err { display: block; background: #fef2f2; border: 1px solid #fecaca; color: #991b1b; }
  /* Create test order */
  .create-order { margin-bottom: 24px; padding: 14px 16px; background: #f8f8ff; border-radius: 8px; border: 1px dashed #d0d5dd; }
  .create-order summary { cursor: pointer; font-size: 0.82rem; font-weight: 600; color: #4f5c65; }
  .create-order summary:hover { color: #222173; }
  .create-order[open] summary { margin-bottom: 12px; }
  .create-body { display: flex; flex-direction: column; gap: 10px; }
  .create-hint { font-size: 0.78rem; color: #6c7086; line-height: 1.5; }
  .create-body input[type="file"] { font-size: 0.85rem; color: #4f5c65; }
  .create-body button { background: #222173; color: #fff; font-size: 0.85rem; padding: 10px; border-radius: 8px; border: none; cursor: pointer; font-weight: 600; transition: opacity 0.2s; }
  .create-body button:hover { opacity: 0.85; }
  .create-body button:disabled { background: #d0d5dd; color: #a0a8b4; cursor: not-allowed; }
  #sample-btn { background: linear-gradient(135deg, #28dbbf, #77bff6); color: #222173; font-size: 0.95rem; font-weight: 700; padding: 12px; width: 100%; }
  #sample-btn:hover { opacity: 0.9; transform: translateY(-1px); }
  #sample-btn:disabled { background: #d0d5dd; color: #a0a8b4; cursor: not-allowed; transform: none; }
  #create-status { display: none; font-size: 0.82rem; padding: 10px 12px; border-radius: 6px; line-height: 1.5; }
  #create-status.info { display: block; background: #e5f1fb; border: 1px solid #b8d4ec; color: #4f5c65; }
  #create-status.ok { display: block; background: #ecfdf5; border: 1px solid #a7f3d0; color: #166534; }
  #create-status.err { display: block; background: #fef2f2; border: 1px solid #fecaca; color: #991b1b; }
  .footer { padding: 20px; text-align: center; font-size: 0.75rem; color: #a0a8b4; }
  .footer a { color: #3bd8be; text-decoration: none; }
  .footer a:hover { text-decoration: underline; }
  /* Tabs */
  .tabs { display: flex; justify-content: center; gap: 0; margin-top: 24px; margin-bottom: -16px; }
  .tab-btn { padding: 10px 28px; border: none; background: transparent; color: #a0a8b4; font-size: 0.85rem; font-weight: 600; cursor: pointer; border-bottom: 3px solid transparent; transition: all 0.2s; }
  .tab-btn:hover { color: #222173; }
  .tab-btn.active { color: #222173; border-bottom-color: #3bd8be; }
  .tab-panel { display: none; width: 100%; flex-direction: column; align-items: center; }
  .tab-panel.active { display: flex; }
  /* Production tab */
  .prod-card { background: #fff; border-radius: 12px; padding: 32px 36px; width: 100%; max-width: 700px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1), 0 2px 4px -2px rgba(0,0,0,0.06); margin-top: 20px; }
  .prod-card h2 { font-size: 1.15rem; font-weight: 700; color: #222173; margin-bottom: 6px; }
  .prod-card .prod-sub { font-size: 0.82rem; color: #4f5c65; margin-bottom: 20px; line-height: 1.5; }
  .prod-card h3 { font-size: 0.9rem; font-weight: 700; color: #222173; margin: 22px 0 10px 0; display: flex; align-items: center; gap: 8px; }
  .prod-card h3 .pill { font-size: 0.65rem; font-weight: 700; padding: 2px 8px; border-radius: 9999px; }
  .prod-card h3 .pill.done { background: linear-gradient(135deg, rgba(59,216,190,0.15), rgba(119,191,246,0.15)); color: #222173; }
  .prod-card h3 .pill.new { background: #222173; color: #fff; }
  .prod-card pre { background: #1e1e2e; color: #cdd6f4; padding: 16px 20px; border-radius: 8px; font-size: 0.78rem; line-height: 1.6; overflow-x: auto; margin: 8px 0 4px 0; }
  .prod-card pre .kw { color: #cba6f7; }
  .prod-card pre .fn { color: #89b4fa; }
  .prod-card pre .str { color: #a6e3a1; }
  .prod-card pre .cmt { color: #6c7086; font-style: italic; }
  .prod-card pre .dec { color: #f9e2af; }
  .prod-card pre .num { color: #fab387; }
  .prod-card .note { font-size: 0.78rem; color: #4f5c65; line-height: 1.5; margin: 6px 0; padding: 10px 14px; background: #f8f8ff; border-radius: 8px; border-left: 3px solid #3bd8be; }
  .prod-card .file-ref { font-size: 0.72rem; color: #a0a8b4; font-family: monospace; margin-bottom: 4px; }
  .test-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: 10px 0; }
  .test-item { padding: 8px 12px; background: #f8f8ff; border-radius: 6px; font-size: 0.76rem; color: #4f5c65; display: flex; align-items: center; gap: 6px; }
  .test-item .pass { color: #3bd8be; font-weight: 700; }
  /* Sentry dashboard */
  .sentry-dash { margin-top: 16px; }
  .sentry-actions { display: flex; gap: 10px; margin-bottom: 14px; }
  .sentry-actions button { padding: 8px 16px; border-radius: 6px; font-size: 0.78rem; font-weight: 600; cursor: pointer; border: none; transition: all 0.2s; }
  .sentry-actions .test-btn { background: #fee2e2; color: #991b1b; }
  .sentry-actions .test-btn:hover { background: #fecaca; }
  .sentry-actions .refresh-btn { background: #e5f1fb; color: #222173; }
  .sentry-actions .refresh-btn:hover { background: #d0e4f5; }
  .sentry-actions .link-btn { background: linear-gradient(135deg, rgba(59,216,190,0.15), rgba(119,191,246,0.15)); color: #222173; text-decoration: none; display: inline-flex; align-items: center; padding: 8px 16px; border-radius: 6px; font-size: 0.78rem; font-weight: 600; }
  .sentry-actions .link-btn:hover { opacity: 0.8; }
  .issue-list { list-style: none; padding: 0; }
  .issue-item { padding: 10px 14px; border-radius: 8px; background: #f8f8ff; margin-bottom: 8px; display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
  .issue-item .issue-main { flex: 1; min-width: 0; }
  .issue-item .issue-title { font-size: 0.8rem; font-weight: 600; color: #991b1b; margin-bottom: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .issue-item .issue-title a { color: inherit; text-decoration: none; }
  .issue-item .issue-title a:hover { text-decoration: underline; }
  .issue-item .issue-culprit { font-size: 0.72rem; color: #6c7086; }
  .issue-item .issue-meta { text-align: right; flex-shrink: 0; }
  .issue-item .issue-count { font-size: 0.85rem; font-weight: 700; color: #222173; }
  .issue-item .issue-time { font-size: 0.68rem; color: #a0a8b4; }
  .issue-level { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; flex-shrink: 0; margin-top: 4px; }
  .issue-level.error { background: #ef4444; }
  .issue-level.warning { background: #f59e0b; }
  .issue-level.info { background: #3b82f6; }
  .sentry-empty { text-align: center; padding: 20px; color: #a0a8b4; font-size: 0.82rem; }
  .sentry-status { font-size: 0.75rem; color: #a0a8b4; margin-top: 8px; text-align: right; }
  /* Azoni Chat Tab */
  .chat-container { background: #fff; border-radius: 12px; width: 100%; max-width: 500px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1), 0 2px 4px -2px rgba(0,0,0,0.06); margin-top: 20px; display: flex; flex-direction: column; height: 600px; }
  .chat-head { padding: 20px 24px 16px; border-bottom: 1px solid #f0f0f5; }
  .chat-head h2 { font-size: 1.15rem; font-weight: 700; color: #222173; margin-bottom: 4px; display: flex; align-items: center; gap: 8px; }
  .chat-head .ai-badge { background: linear-gradient(135deg, #3bd8be, #77bff6); color: #222173; font-size: 0.65rem; font-weight: 700; padding: 2px 8px; border-radius: 9999px; }
  .chat-head p { font-size: 0.82rem; color: #4f5c65; line-height: 1.4; }
  .chat-messages { flex: 1; overflow-y: auto; padding: 16px 24px; display: flex; flex-direction: column; gap: 12px; }
  .chat-msg { max-width: 85%; padding: 10px 14px; border-radius: 12px; font-size: 0.85rem; line-height: 1.5; word-wrap: break-word; }
  .chat-msg.user { align-self: flex-end; background: linear-gradient(135deg, #222173, #2d2b8a); color: #fff; border-bottom-right-radius: 4px; }
  .chat-msg.assistant { align-self: flex-start; background: #f5f6ff; color: #222173; border-bottom-left-radius: 4px; }
  .chat-msg.assistant code { background: #e5e7f0; padding: 1px 5px; border-radius: 3px; font-size: 0.8rem; }
  .chat-msg.err-msg { align-self: center; background: #fef2f2; color: #991b1b; font-size: 0.8rem; border: 1px solid #fecaca; }
  .chat-typing { align-self: flex-start; color: #a0a8b4; font-size: 0.8rem; font-style: italic; padding: 4px 0; }
  .chat-chips { padding: 12px 24px; display: flex; flex-wrap: wrap; gap: 6px; border-top: 1px solid #f0f0f5; }
  .chat-chips button { padding: 6px 12px; border: 1px solid #d0d5dd; border-radius: 9999px; background: #fff; color: #4f5c65; font-size: 0.72rem; cursor: pointer; transition: all 0.2s; width: auto; font-weight: 500; }
  .chat-chips button:hover { border-color: #3bd8be; color: #222173; background: rgba(59,216,190,0.05); }
  .chat-input-row { padding: 12px 16px; border-top: 1px solid #f0f0f5; display: flex; gap: 8px; }
  .chat-input-row input { flex: 1; margin-bottom: 0; padding: 10px 14px; font-size: 0.9rem; }
  .chat-input-row button { width: auto; padding: 10px 20px; font-size: 0.85rem; white-space: nowrap; }
</style>
</head>
<body>
<div class="topbar">
  <svg viewBox="0 0 120 28" fill="none" xmlns="http://www.w3.org/2000/svg">
    <circle cx="14" cy="14" r="10" fill="url(#g)"/>
    <path d="M10 14l3 3 5-6" stroke="#222173" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
    <defs><linearGradient id="g" x1="4" y1="4" x2="24" y2="24"><stop stop-color="#3bd8be"/><stop offset="1" stop-color="#77bff6"/></linearGradient></defs>
  </svg>
  <span>autoenhance.ai</span>
  <span class="badge">BATCH API</span>
</div>
<div class="tabs">
  <button class="tab-btn active" onclick="switchTab('interview')">Interview Submission</button>
  <button class="tab-btn" onclick="switchTab('production')">Production Version</button>
  <button class="tab-btn" onclick="switchTab('chat')">Ask Azoni AI</button>
</div>
<main>
<div class="tab-panel active" id="tab-interview">
<div class="card">
  <h1>Batch Image Downloader</h1>
  <p class="sub">Download all enhanced images for an order as a ZIP</p>
  <details id="create-section" class="create-order">
    <summary>Need a test order? Try it here</summary>
    <div class="create-body">
      <button type="button" id="sample-btn" onclick="createSampleOrder()">Try Demo &mdash; Use Sample Images</button>
      <p class="create-hint" style="text-align:center;color:#a0a8b4;font-size:0.72rem;">Uploads 3 bundled real-estate photos to Autoenhance. No files needed.</p>
      <div style="display:flex;align-items:center;gap:10px;margin:4px 0 2px 0;"><hr style="flex:1;border:none;border-top:1px solid #e0e0ea;"><span style="font-size:0.72rem;color:#a0a8b4;">or upload your own</span><hr style="flex:1;border:none;border-top:1px solid #e0e0ea;"></div>
      <input type="file" id="upload_files" multiple accept=".jpg,.jpeg,.png,.webp">
      <button type="button" id="create-btn" onclick="createTestOrder()">Upload &amp; Create Order</button>
      <div id="create-status"></div>
    </div>
  </details>
  <form id="form">
    <label for="order_id">Order ID</label>
    <input type="text" id="order_id" placeholder="e.g. 100aefc4-8664-4180-9a97-42f428c6aace" required>
    <div class="row">
      <div>
        <label for="format">Format</label>
        <select id="format">
          <option value="jpeg" selected>JPEG</option>
          <option value="png">PNG</option>
          <option value="webp">WebP</option>
        </select>
      </div>
      <div>
        <label for="quality">Quality (1-90)</label>
        <input type="number" id="quality" min="1" max="90" placeholder="Default">
      </div>
    </div>
    <div class="checks">
      <label><input type="checkbox" id="dev_mode" checked> Dev mode (free, watermarked)</label>
      <label><input type="checkbox" id="preview" checked> Preview quality</label>
    </div>
    <button type="submit" id="btn">Download ZIP</button>
  </form>
  <div id="status"></div>
</div>
<div class="info-panel">
  <details open>
    <summary>How it works &mdash; Batch vs standard endpoints</summary>

    <div class="compare">
      <div class="col left">
        <strong>Standard endpoint</strong>
        1 request &rarr; 1 image<br>
        Fast, all-or-nothing<br>
        Small payload, simple errors
      </div>
      <div class="col right">
        <strong>Batch endpoint (this)</strong>
        1 request &rarr; N images<br>
        Partial success is valid<br>
        Needs concurrency &amp; throttling
      </div>
    </div>

    <h3>Design decisions</h3>
    <ul class="handled">
      <li><span class="check">&#10003;</span><span><strong>Concurrency</strong> &mdash; Downloads run in parallel via <code>asyncio.gather</code> with a semaphore (max 5) to balance speed vs. API rate limits.</span></li>
      <li><span class="check">&#10003;</span><span><strong>Partial failure</strong> &mdash; If some images fail (still processing, timed out), the ZIP includes what succeeded plus a <code>_download_report.txt</code>.</span></li>
      <li><span class="check">&#10003;</span><span><strong>Timeouts</strong> &mdash; 60s per image. Response headers report total / downloaded / failed counts.</span></li>
      <li><span class="check">&#10003;</span><span><strong>Response format</strong> &mdash; ZIP chosen over multipart (poor client support) or base64 JSON (33% size overhead). Universal support, good compression.</span></li>
      <li><span class="check">&#10003;</span><span><strong>Redirects</strong> &mdash; Autoenhance returns 302 &rarr; asset server &rarr; S3. The client follows these transparently.</span></li>
      <li><span class="check">&#10003;</span><span><strong>Memory</strong> &mdash; Images buffer in-memory before ZIP creation. Fine for typical orders (&lt;50 images); larger orders would need streaming or an async job.</span></li>
    </ul>
  </details>
</div>
<div class="info-panel">
  <details>
    <summary>Assumptions &amp; open questions</summary>

    <ul class="handled">
      <li><span class="assume">?</span><span><strong>Upload pipeline is external</strong> &mdash; This endpoint is downstream of whatever upload flow the client uses (web app, SDK, direct API). We accept an <code>order_id</code> after images are already uploaded and enhanced.</span></li>
      <li><span class="assume">?</span><span><strong>Enhancement timing is unknown</strong> &mdash; Images may still be processing when the batch endpoint is called. Handled gracefully via partial success + failure report, but we can't trigger or wait for completion.</span></li>
      <li><span class="assume">?</span><span><strong>Order schema is partially documented</strong> &mdash; We check for both <code>image_id</code>/<code>id</code> and <code>image_name</code>/<code>name</code> to handle field name variations in the API response.</span></li>
      <li><span class="assume">?</span><span><strong>No completion webhook</strong> &mdash; Without a callback, the caller must wait or poll before requesting the batch download.</span></li>
      <li><span class="assume">?</span><span><strong>Rate limits undocumented</strong> &mdash; We default to 5 concurrent downloads. The actual Autoenhance limit isn't published, so this can be tuned with production data.</span></li>
    </ul>

    <h3>Possible extensions</h3>
    <ul class="handled">
      <li><span class="check">&#8594;</span><span><strong>Poll-until-ready</strong> &mdash; A <code>wait=true</code> param that retries until all images are processed before downloading.</span></li>
      <li><span class="check">&#8594;</span><span><strong>Async job pattern</strong> &mdash; <code>POST /jobs</code> starts the download and returns a job ID; <code>GET /jobs/{id}</code> returns status or the ZIP.</span></li>
      <li><span class="check">&#8594;</span><span><strong>Webhook integration</strong> &mdash; Auto-trigger the batch download when Autoenhance signals an order is complete.</span></li>
    </ul>
  </details>
</div>
<div class="info-panel">
  <details>
    <summary>Production considerations</summary>

    <h3>Observability</h3>
    <ul class="handled">
      <li><span class="prod cur">&#9679;</span><span><strong>Structured logging</strong> &mdash; Python <code>logging</code> at INFO/WARNING/ERROR. Logs order retrieval, per-image status, and final counts.</span></li>
      <li><span class="prod next">&#9675;</span><span><strong>Distributed tracing</strong> &mdash; OpenTelemetry spans for the order fetch and each image download. Shows where time is spent (upstream latency vs. ZIP creation).</span></li>
      <li><span class="prod cur">&#9679;</span><span><strong>Error tracking</strong> &mdash; Sentry SDK captures unhandled exceptions with request context. Activated via <code>SENTRY_DSN</code> env var; no-op when unset.</span></li>
    </ul>

    <h3>Metrics</h3>
    <ul class="handled">
      <li><span class="prod next">&#9675;</span><span><strong>Request latency</strong> &mdash; P50/P95/P99 for the batch endpoint, plus per-image latency (scales with order size).</span></li>
      <li><span class="prod next">&#9675;</span><span><strong>Success/failure rates</strong> &mdash; Full success, partial success, and total failure. Alert on partial-failure spikes (may signal processing delays).</span></li>
      <li><span class="prod next">&#9675;</span><span><strong>Upstream health</strong> &mdash; Track Autoenhance API response times and errors separately. Detect degradation before users report it.</span></li>
      <li><span class="prod next">&#9675;</span><span><strong>ZIP size distribution</strong> &mdash; Monitor payload sizes to catch memory pressure early.</span></li>
    </ul>

    <h3>Versioning</h3>
    <ul class="handled">
      <li><span class="prod cur">&#9679;</span><span><strong>Autoenhance API</strong> &mdash; Pinned to <code>/v3</code>. A v4 release won't break us until we explicitly migrate.</span></li>
      <li><span class="prod next">&#9675;</span><span><strong>Our own API</strong> &mdash; Currently unversioned. Add a <code>/v1/</code> prefix so the response format can evolve without breaking callers.</span></li>
      <li><span class="prod next">&#9675;</span><span><strong>Dependency pinning</strong> &mdash; Exact versions in <code>requirements.txt</code>. Add <code>pip-tools</code> for reproducible builds with transitive deps.</span></li>
    </ul>

    <h3>Security</h3>
    <ul class="handled">
      <li><span class="prod cur">&#9679;</span><span><strong>API key isolation</strong> &mdash; Stored in env var, never committed. <code>.gitignore</code> excludes <code>.env</code>.</span></li>
      <li><span class="prod cur">&#9679;</span><span><strong>Input validation</strong> &mdash; Order ID validated as UUID before any upstream call. Malformed input returns 400 immediately.</span></li>
      <li><span class="prod next">&#9675;</span><span><strong>Rate limiting</strong> &mdash; Each batch call fans out to N upstream requests. Add per-IP throttling to prevent quota exhaustion.</span></li>
      <li><span class="prod next">&#9675;</span><span><strong>Authentication</strong> &mdash; Endpoint is currently public. Add API key or OAuth to control who triggers downloads (and consumes credits).</span></li>
    </ul>

    <h3>Testing</h3>
    <ul class="handled">
      <li><span class="prod cur">&#9679;</span><span><strong>Manual E2E</strong> &mdash; Verified with a real 3-image order against the live API, locally and on Render.</span></li>
      <li><span class="prod cur">&#9679;</span><span><strong>Unit tests</strong> &mdash; 13 tests via <code>httpx</code> mock transport. Covers validation, success/partial/total failure, edge cases, health, and UI.</span></li>
      <li><span class="prod next">&#9675;</span><span><strong>Integration tests</strong> &mdash; Real API calls via <code>x-dev-mode</code> (no credits). Assert ZIP contents and structure.</span></li>
      <li><span class="prod next">&#9675;</span><span><strong>Load testing</strong> &mdash; Stress-test with large orders (50+ images) and concurrent callers to find memory and timeout limits.</span></li>
    </ul>

    <h3>Operational</h3>
    <ul class="handled">
      <li><span class="prod cur">&#9679;</span><span><strong>Health check</strong> &mdash; <code>/health</code> reports status and API key config. Monitored by UptimeRobot.</span></li>
      <li><span class="prod next">&#9675;</span><span><strong>Graceful degradation</strong> &mdash; Return 503 with Retry-After when Autoenhance is down, instead of timing out. Circuit breaker pattern.</span></li>
      <li><span class="prod next">&#9675;</span><span><strong>Caching</strong> &mdash; Enhanced images are immutable. Cache by image ID + format + quality to skip redundant downloads.</span></li>
      <li><span class="prod next">&#9675;</span><span><strong>CI/CD</strong> &mdash; Auto-deploys from <code>main</code> via Render. Add a test gate and staging environment.</span></li>
    </ul>
  </details>
</div>
</div><!-- /tab-interview -->

<div class="tab-panel" id="tab-production">
<div class="prod-card">
  <h2>Production-Hardened Version</h2>
  <p class="prod-sub">Production-grade additions to the batch endpoint: input validation, unit tests, error tracking, and patterns for circuit breaking, caching, and rate limiting. The Interview tab is the primary submission.</p>

  <h3>UUID Input Validation <span class="pill done">IMPLEMENTED</span></h3>
  <p class="file-ref">app.py &mdash; batch_download_order_images()</p>
  <pre><span class="cmt"># Validate order_id is a UUID before making upstream calls</span>
<span class="kw">import</span> re
uuid_pattern = re.compile(
    <span class="str">r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"</span>,
    re.IGNORECASE,
)
<span class="kw">if not</span> uuid_pattern.match(order_id):
    <span class="kw">raise</span> HTTPException(
        status_code=<span class="num">400</span>,
        detail=<span class="str">f"Invalid order ID format. Expected UUID, got: '{order_id}'"</span>,
    )</pre>
  <div class="note">Rejects bad input before any upstream call. Prevents wasted round-trips and injection vectors.</div>

  <h3>Unit Test Suite <span class="pill done">IMPLEMENTED</span></h3>
  <p class="file-ref">test_app.py &mdash; 13 tests, all passing</p>
  <div class="test-grid">
    <div class="test-item"><span class="pass">&#10003;</span> Invalid UUID rejected</div>
    <div class="test-item"><span class="pass">&#10003;</span> SQL injection blocked</div>
    <div class="test-item"><span class="pass">&#10003;</span> Full success &rarr; ZIP</div>
    <div class="test-item"><span class="pass">&#10003;</span> Correct filenames in ZIP</div>
    <div class="test-item"><span class="pass">&#10003;</span> Partial failure + report</div>
    <div class="test-item"><span class="pass">&#10003;</span> All fail &rarr; 422</div>
    <div class="test-item"><span class="pass">&#10003;</span> Empty order &rarr; 404</div>
    <div class="test-item"><span class="pass">&#10003;</span> Order not found</div>
    <div class="test-item"><span class="pass">&#10003;</span> Duplicate names deduped</div>
    <div class="test-item"><span class="pass">&#10003;</span> Health endpoint</div>
    <div class="test-item"><span class="pass">&#10003;</span> UI returns HTML</div>
    <div class="test-item"><span class="pass">&#10003;</span> Valid UUID passes through</div>
  </div>
  <pre><span class="cmt"># Mock strategy: subclass httpx.AsyncClient with MockTransport</span>
<span class="kw">class</span> <span class="fn">MockedAsyncClient</span>(httpx.AsyncClient):
    <span class="kw">def</span> <span class="fn">__init__</span>(self, **kwargs):
        kwargs.pop(<span class="str">"timeout"</span>, <span class="kw">None</span>)
        kwargs.pop(<span class="str">"follow_redirects"</span>, <span class="kw">None</span>)
        <span class="fn">super</span>().__init__(transport=transport, **kwargs)

<span class="cmt"># monkeypatch replaces httpx.AsyncClient per-test</span>
monkeypatch.setattr(httpx, <span class="str">"AsyncClient"</span>, make_mock_client(...))</pre>
  <div class="note"><code>httpx.MockTransport</code> intercepts all outgoing HTTP &mdash; no real API calls, no credits consumed. Each test configures its own mock responses for full isolation.</div>

  <h3>Sentry Error Tracking <span class="pill done">IMPLEMENTED</span></h3>
  <p class="file-ref">app.py &mdash; startup config</p>
  <pre><span class="kw">import</span> sentry_sdk

<span class="cmt"># Only active when DSN is configured (no-op otherwise)</span>
<span class="kw">if</span> os.getenv(<span class="str">"SENTRY_DSN"</span>):
    sentry_sdk.init(
        dsn=os.getenv(<span class="str">"SENTRY_DSN"</span>),
        traces_sample_rate=<span class="num">0.2</span>,
        environment=os.getenv(<span class="str">"SENTRY_ENV"</span>, <span class="str">"production"</span>),
    )</pre>
  <div class="note">FastAPI integration is automatic &mdash; Sentry captures unhandled exceptions with full request context. 20% trace sampling keeps costs low. No-op when <code>SENTRY_DSN</code> is unset.</div>

  <div class="sentry-dash">
    <h3>Live Error Dashboard <span class="pill done">LIVE</span></h3>
    <div class="sentry-actions">
      <button class="test-btn" onclick="triggerSentryTest()">Trigger Test Error</button>
      <button class="refresh-btn" onclick="loadSentryIssues()">Refresh</button>
      <a class="link-btn" href="https://azoni.sentry.io/issues/?project=4510914036826112" target="_blank">Open Sentry &rarr;</a>
    </div>
    <div id="sentry-test-status" style="display:none; font-size:0.78rem; padding:8px 12px; border-radius:6px; margin-bottom:10px;"></div>
    <ul class="issue-list" id="sentry-issues">
      <li class="sentry-empty">Loading issues...</li>
    </ul>
    <div class="sentry-status" id="sentry-status"></div>
  </div>

  <h3>Circuit Breaker Pattern <span class="pill new">NEXT STEP</span></h3>
  <p class="file-ref">How it would integrate into app.py</p>
  <pre><span class="kw">class</span> <span class="fn">CircuitBreaker</span>:
    <span class="kw">def</span> <span class="fn">__init__</span>(self, failure_threshold=<span class="num">5</span>, reset_timeout=<span class="num">60</span>):
        self.failures = <span class="num">0</span>
        self.threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.state = <span class="str">"closed"</span>  <span class="cmt"># closed | open | half-open</span>
        self.last_failure_time = <span class="num">0</span>

    <span class="kw">def</span> <span class="fn">record_failure</span>(self):
        self.failures += <span class="num">1</span>
        <span class="kw">if</span> self.failures >= self.threshold:
            self.state = <span class="str">"open"</span>
            self.last_failure_time = time.time()

    <span class="kw">def</span> <span class="fn">allow_request</span>(self) -> bool:
        <span class="kw">if</span> self.state == <span class="str">"closed"</span>: <span class="kw">return True</span>
        <span class="kw">if</span> time.time() - self.last_failure_time > self.reset_timeout:
            self.state = <span class="str">"half-open"</span>
            <span class="kw">return True</span>
        <span class="kw">return False</span>

<span class="cmt"># In the endpoint:</span>
<span class="kw">if not</span> circuit_breaker.allow_request():
    <span class="kw">raise</span> HTTPException(<span class="num">503</span>, detail=<span class="str">"Autoenhance API temporarily unavailable"</span>,
        headers={<span class="str">"Retry-After"</span>: <span class="str">"60"</span>})</pre>
  <div class="note">Prevents cascading failures. After 5 consecutive errors, returns 503 immediately for 60s instead of letting every request time out.</div>

  <h3>Response Caching <span class="pill new">NEXT STEP</span></h3>
  <p class="file-ref">Image-level cache by (image_id, format, quality)</p>
  <pre><span class="kw">from</span> functools <span class="kw">import</span> lru_cache
<span class="kw">from</span> hashlib <span class="kw">import</span> sha256

<span class="cmt"># Enhanced images are immutable once processed</span>
image_cache: dict[str, bytes] = {}

<span class="kw">def</span> <span class="fn">cache_key</span>(image_id: str, fmt: str, quality: int | None) -> str:
    <span class="kw">return</span> <span class="str">f"{image_id}:{fmt}:{quality or 'default'}"</span>

<span class="kw">async def</span> <span class="fn">download_image</span>(image: dict) -> dict:
    key = cache_key(image_id, image_format, quality)
    <span class="kw">if</span> key <span class="kw">in</span> image_cache:
        logger.info(<span class="str">"Cache HIT for %s"</span>, image_id)
        <span class="kw">return</span> {<span class="str">"content"</span>: image_cache[key], ...}
    <span class="cmt"># ... download as before ...</span>
    image_cache[key] = resp.content  <span class="cmt"># store on success</span></pre>
  <div class="note">Enhanced images are immutable once processed, so caching avoids redundant downloads. In production, swap the in-memory dict for Redis or Memcached with a TTL.</div>

  <h3>Rate Limiting Our Endpoint <span class="pill new">NEXT STEP</span></h3>
  <pre><span class="cmt"># Using slowapi (built on limits library)</span>
<span class="kw">from</span> slowapi <span class="kw">import</span> Limiter
<span class="kw">from</span> slowapi.util <span class="kw">import</span> get_remote_address

limiter = Limiter(key_func=get_remote_address)

<span class="dec">@app.get</span>(<span class="str">"/orders/{order_id}/images"</span>)
<span class="dec">@limiter.limit</span>(<span class="str">"10/minute"</span>)
<span class="kw">async def</span> <span class="fn">batch_download_order_images</span>(request: Request, ...):
    ...</pre>
  <div class="note">Each batch call fans out to N upstream requests. Without throttling, one caller could exhaust API quotas. 10 req/min per IP is a reasonable default.</div>

  <h3>Async Job Pattern for Large Orders <span class="pill new">NEXT STEP</span></h3>
  <pre><span class="cmt"># POST /jobs &mdash; start download, return immediately</span>
<span class="dec">@app.post</span>(<span class="str">"/jobs"</span>)
<span class="kw">async def</span> <span class="fn">create_job</span>(order_id: str):
    job_id = str(uuid4())
    jobs[job_id] = {<span class="str">"status"</span>: <span class="str">"processing"</span>, <span class="str">"order_id"</span>: order_id}
    asyncio.create_task(process_job(job_id, order_id))
    <span class="kw">return</span> {<span class="str">"job_id"</span>: job_id, <span class="str">"status"</span>: <span class="str">"processing"</span>}

<span class="cmt"># GET /jobs/{job_id} &mdash; poll for result</span>
<span class="dec">@app.get</span>(<span class="str">"/jobs/{job_id}"</span>)
<span class="kw">async def</span> <span class="fn">get_job</span>(job_id: str):
    job = jobs.get(job_id)
    <span class="kw">if</span> job[<span class="str">"status"</span>] == <span class="str">"complete"</span>:
        <span class="kw">return</span> StreamingResponse(job[<span class="str">"zip_data"</span>], ...)
    <span class="kw">return</span> {<span class="str">"status"</span>: job[<span class="str">"status"</span>], <span class="str">"progress"</span>: job.get(<span class="str">"progress"</span>)}</pre>
  <div class="note">Large orders (50+ images) risk HTTP timeouts. The job pattern lets the client fire-and-forget, then poll. Back with a task queue (Celery / RQ) and object storage in production.</div>

</div>
</div><!-- /tab-production -->

<div class="tab-panel" id="tab-chat">
<div class="chat-container">
  <div class="chat-head">
    <h2>Azoni AI <span class="ai-badge">CHATBOT</span></h2>
    <p>Ask me anything about this batch endpoint, the design decisions, or Charlton's background.</p>
  </div>
  <div class="chat-messages" id="chat-messages">
    <div class="chat-msg assistant">Hi! I'm Azoni AI, Charlton's portfolio chatbot. I know all about this batch image downloader &mdash; the design decisions, error handling, tech stack, and more. Ask me anything!</div>
  </div>
  <div class="chat-chips" id="chat-chips">
    <button onclick="askChip(this)">What does this batch endpoint do?</button>
    <button onclick="askChip(this)">What design decisions were made?</button>
    <button onclick="askChip(this)">How are errors handled?</button>
    <button onclick="askChip(this)">What would you add for production?</button>
    <button onclick="askChip(this)">Tell me about Charlton's background</button>
    <button onclick="askChip(this)">Why did Charlton build it this way?</button>
  </div>
  <div class="chat-input-row">
    <input type="text" id="chat-input" placeholder="Ask about the endpoint, design, or Charlton..." onkeydown="if(event.key==='Enter')sendChat()">
    <button onclick="sendChat()" id="chat-send-btn">Send</button>
  </div>
</div>
</div><!-- /tab-chat -->

</main>
<div class="footer">Batch endpoint for <a href="https://autoenhance.ai" target="_blank">autoenhance.ai</a> &mdash; <a href="/docs">API Docs</a></div>
<script>
const form = document.getElementById('form');
const btn = document.getElementById('btn');
const status = document.getElementById('status');

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const orderId = document.getElementById('order_id').value.trim();
  if (!orderId) return;

  const params = new URLSearchParams();
  params.set('format', document.getElementById('format').value);
  params.set('dev_mode', document.getElementById('dev_mode').checked);
  params.set('preview', document.getElementById('preview').checked);
  const q = document.getElementById('quality').value;
  if (q) params.set('quality', q);

  btn.disabled = true;
  btn.textContent = 'Downloading...';
  status.className = 'info';
  status.style.display = 'block';
  status.textContent = 'Fetching images from Autoenhance\u2026';

  try {
    const resp = await fetch(`/orders/${orderId}/images?${params}`);
    if (!resp.ok) {
      const err = await resp.json();
      if (resp.status === 422) {
        const n = err.detail?.failures?.length || 0;
        throw new Error('Images are still being enhanced by Autoenhance (' + n + ' not ready). Wait 30\u201360 seconds and try again.');
      }
      throw new Error(err.detail?.message || err.detail || `HTTP ${resp.status}`);
    }
    const total = resp.headers.get('X-Total-Images') || '?';
    const downloaded = resp.headers.get('X-Downloaded') || '?';
    const failed = resp.headers.get('X-Failed') || '0';

    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `order_${orderId.slice(0,8)}_images.zip`;
    a.click();
    URL.revokeObjectURL(url);

    status.className = 'ok';
    status.textContent = `${downloaded}/${total} images downloaded.` + (parseInt(failed) > 0 ? ` ${failed} failed \u2014 see report in ZIP.` : '');
  } catch (err) {
    status.className = 'err';
    status.textContent = err.message;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Download ZIP';
  }
});

async function createTestOrder() {
  const fileInput = document.getElementById('upload_files');
  const files = fileInput.files;
  if (!files.length) { alert('Select at least one image'); return; }

  const createBtn = document.getElementById('create-btn');
  const cs = document.getElementById('create-status');
  createBtn.disabled = true;
  createBtn.textContent = 'Uploading...';
  cs.className = 'info';
  cs.textContent = 'Uploading ' + files.length + ' image(s) to Autoenhance...';

  const formData = new FormData();
  for (const f of files) formData.append('files', f);

  try {
    const resp = await fetch('/api/create-order', { method: 'POST', body: formData });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || 'HTTP ' + resp.status);

    document.getElementById('order_id').value = data.order_id;
    cs.className = 'ok';
    cs.innerHTML = '<strong>' + data.order_id + '</strong><br>' +
      data.images_uploaded + ' image(s) uploaded. Allow ~60s for processing, then hit Download ZIP.';
  } catch (err) {
    cs.className = 'err';
    cs.textContent = err.message;
  } finally {
    createBtn.disabled = false;
    createBtn.textContent = 'Upload & Create Order';
  }
}

async function createSampleOrder() {
  const sampleBtn = document.getElementById('sample-btn');
  const cs = document.getElementById('create-status');
  sampleBtn.disabled = true;
  sampleBtn.textContent = 'Creating order with sample images...';
  cs.className = 'info';
  cs.textContent = 'Uploading 3 sample real-estate photos to Autoenhance...';

  try {
    const resp = await fetch('/api/create-sample-order', { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || 'HTTP ' + resp.status);

    document.getElementById('order_id').value = data.order_id;
    cs.className = 'ok';
    cs.innerHTML = '<strong>' + data.order_id + '</strong><br>' +
      data.images_uploaded + ' sample image(s) uploaded. Allow ~60s for processing, then hit Download ZIP.';
  } catch (err) {
    cs.className = 'err';
    cs.textContent = err.message;
  } finally {
    sampleBtn.disabled = false;
    sampleBtn.textContent = 'Try Demo \u2014 Use Sample Images';
  }
}

function switchTab(tab) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.getElementById('tab-' + tab).classList.add('active');
  event.target.classList.add('active');
  if (tab === 'production' && !window._sentryLoaded) {
    window._sentryLoaded = true;
    loadSentryIssues();
  }
}

async function loadSentryIssues() {
  const list = document.getElementById('sentry-issues');
  const statusEl = document.getElementById('sentry-status');
  list.innerHTML = '<li class="sentry-empty">Loading...</li>';
  try {
    const resp = await fetch('/api/sentry/issues');
    const data = await resp.json();
    if (data.error) {
      list.innerHTML = '<li class="sentry-empty">' + data.error + '</li>';
      return;
    }
    if (!data.issues.length) {
      list.innerHTML = '<li class="sentry-empty">No issues yet &mdash; your app is clean!</li>';
      statusEl.textContent = 'Updated ' + new Date().toLocaleTimeString();
      return;
    }
    list.innerHTML = data.issues.map(i => {
      const level = i.level || 'error';
      const ago = timeAgo(i.lastSeen);
      return '<li class="issue-item">' +
        '<span class="issue-level ' + level + '"></span>' +
        '<div class="issue-main">' +
          '<div class="issue-title"><a href="' + (i.permalink || '#') + '" target="_blank">' + escHtml(i.title) + '</a></div>' +
          '<div class="issue-culprit">' + escHtml(i.culprit || '') + '</div>' +
        '</div>' +
        '<div class="issue-meta">' +
          '<div class="issue-count">' + i.count + 'x</div>' +
          '<div class="issue-time">' + ago + '</div>' +
        '</div></li>';
    }).join('');
    statusEl.textContent = 'Updated ' + new Date().toLocaleTimeString();
  } catch (e) {
    list.innerHTML = '<li class="sentry-empty">Failed to load: ' + e.message + '</li>';
  }
}

async function triggerSentryTest() {
  const s = document.getElementById('sentry-test-status');
  s.style.display = 'block';
  s.style.background = '#fef2f2';
  s.style.border = '1px solid #fecaca';
  s.style.color = '#991b1b';
  s.textContent = 'Triggering test error...';
  try {
    await fetch('/sentry-debug');
  } catch(e) {}
  s.textContent = 'Test error sent to Sentry! Refresh in a few seconds to see it below.';
  s.style.background = '#ecfdf5';
  s.style.border = '1px solid #a7f3d0';
  s.style.color = '#166534';
  setTimeout(() => loadSentryIssues(), 4000);
}

function timeAgo(dateStr) {
  if (!dateStr) return '';
  const diff = (Date.now() - new Date(dateStr).getTime()) / 1000;
  if (diff < 60) return Math.floor(diff) + 's ago';
  if (diff < 3600) return Math.floor(diff/60) + 'm ago';
  if (diff < 86400) return Math.floor(diff/3600) + 'h ago';
  return Math.floor(diff/86400) + 'd ago';
}

function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

// ===== Azoni AI Chat =====
const AZONI_API = 'https://azoni.ai/.netlify/functions/chat';
let chatHistory = [];

function askChip(btn) {
  document.getElementById('chat-input').value = btn.textContent;
  sendChat();
}

async function sendChat() {
  const input = document.getElementById('chat-input');
  const msg = input.value.trim();
  if (!msg) return;
  input.value = '';

  addChatMsg('user', msg);

  const chips = document.getElementById('chat-chips');
  if (chips) chips.style.display = 'none';

  chatHistory.push({ role: 'user', content: msg });

  const typing = document.createElement('div');
  typing.className = 'chat-typing';
  typing.id = 'chat-typing';
  typing.textContent = 'Azoni AI is thinking\u2026';
  document.getElementById('chat-messages').appendChild(typing);
  scrollChatToBottom();

  const sendBtn = document.getElementById('chat-send-btn');
  sendBtn.disabled = true;

  try {
    const resp = await fetch(AZONI_API, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        messages: chatHistory,
        mode: 'professional',
        model: 'openai/gpt-4o-mini',
        context: 'autoenhance-interview'
      })
    });
    const t = document.getElementById('chat-typing');
    if (t) t.remove();

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.error || 'API error: ' + resp.status);
    }

    const data = await resp.json();
    const reply = data.choices?.[0]?.message?.content || 'Sorry, I could not generate a response.';
    chatHistory.push({ role: 'assistant', content: reply });
    addChatMsg('assistant', reply);
  } catch (err) {
    const t = document.getElementById('chat-typing');
    if (t) t.remove();
    addChatMsg('err-msg', 'Failed to reach Azoni AI: ' + err.message);
  } finally {
    sendBtn.disabled = false;
    input.focus();
  }
}

function addChatMsg(role, content) {
  const container = document.getElementById('chat-messages');
  const div = document.createElement('div');
  div.className = 'chat-msg ' + role;
  if (role === 'assistant') {
    let html = escHtml(content);
    html = html.replace(/\\*\\*(.*?)\\*\\*/g, '<strong>$1</strong>');
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    html = html.replace(/\\n/g, '<br>');
    div.innerHTML = html;
  } else {
    div.textContent = content;
  }
  container.appendChild(div);
  scrollChatToBottom();
}

function scrollChatToBottom() {
  const c = document.getElementById('chat-messages');
  c.scrollTop = c.scrollHeight;
}
</script>
</body>
</html>"""


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
