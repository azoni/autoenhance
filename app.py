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
from fastapi.responses import HTMLResponse, StreamingResponse
from pathlib import Path

load_dotenv(Path(__file__).resolve().parent / ".env")

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


@app.get("/", response_class=HTMLResponse)
async def ui():
    """Simple web UI for testing the batch download endpoint."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Autoenhance Batch Downloader</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f1117; color: #e1e4e8; min-height: 100vh; display: flex; align-items: center; justify-content: center; }
  .card { background: #1c1f2b; border-radius: 12px; padding: 40px; width: 100%; max-width: 480px; box-shadow: 0 8px 32px rgba(0,0,0,0.4); }
  h1 { font-size: 1.4rem; margin-bottom: 4px; }
  .sub { color: #8b949e; font-size: 0.85rem; margin-bottom: 28px; }
  label { display: block; font-size: 0.8rem; color: #8b949e; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.5px; }
  input, select { width: 100%; padding: 10px 12px; border: 1px solid #30363d; border-radius: 8px; background: #161b22; color: #e1e4e8; font-size: 0.95rem; margin-bottom: 18px; outline: none; transition: border-color 0.2s; }
  input:focus, select:focus { border-color: #58a6ff; }
  .row { display: flex; gap: 12px; }
  .row > div { flex: 1; }
  .checks { display: flex; gap: 20px; margin-bottom: 22px; }
  .checks label { display: flex; align-items: center; gap: 6px; text-transform: none; font-size: 0.9rem; color: #e1e4e8; cursor: pointer; }
  .checks input { width: auto; margin: 0; }
  button { width: 100%; padding: 12px; border: none; border-radius: 8px; background: #238636; color: #fff; font-size: 1rem; font-weight: 600; cursor: pointer; transition: background 0.2s; }
  button:hover { background: #2ea043; }
  button:disabled { background: #30363d; color: #8b949e; cursor: not-allowed; }
  #status { margin-top: 16px; padding: 12px; border-radius: 8px; font-size: 0.85rem; display: none; }
  #status.info { display: block; background: #161b22; border: 1px solid #30363d; color: #8b949e; }
  #status.ok { display: block; background: #0d1117; border: 1px solid #238636; color: #3fb950; }
  #status.err { display: block; background: #0d1117; border: 1px solid #da3633; color: #f85149; }
</style>
</head>
<body>
<div class="card">
  <h1>Autoenhance Batch Downloader</h1>
  <p class="sub">Download all enhanced images for an order as a ZIP</p>
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
      <label><input type="checkbox" id="preview" checked> Preview</label>
    </div>
    <button type="submit" id="btn">Download ZIP</button>
  </form>
  <div id="status"></div>
</div>
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
  status.textContent = 'Fetching images from Autoenhance — this may take a moment...';

  try {
    const resp = await fetch(`/orders/${orderId}/images?${params}`);
    if (!resp.ok) {
      const err = await resp.json();
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
    status.textContent = `Done — ${downloaded}/${total} images downloaded.` + (parseInt(failed) > 0 ? ` ${failed} failed (see report in ZIP).` : '');
  } catch (err) {
    status.className = 'err';
    status.textContent = err.message;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Download ZIP';
  }
});
</script>
</body>
</html>"""


@app.get("/health")
async def health_check():
    """Health check — also indicates whether the API key is configured."""
    return {
        "status": "ok",
        "api_key_configured": bool(os.getenv("AUTOENHANCE_API_KEY")),
    }
