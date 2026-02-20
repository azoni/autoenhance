# Autoenhance Batch Image Downloader

A FastAPI service that downloads all enhanced images for a given Autoenhance order and returns them as a ZIP archive.

> **[Live demo](https://autoenhance.onrender.com)** — try the endpoint with sample images, view error tracking, and see the production hardening details (security, observability, architecture decisions) in the **Production Version** tab.

## The Endpoint

```
GET /orders/{order_id}/images?format=jpeg&quality=80&preview=true&dev_mode=true
```

Returns a ZIP archive containing all enhanced images for the given order.

| Parameter  | Type   | Default | Description                                             |
|------------|--------|---------|---------------------------------------------------------|
| `order_id` | path   | —       | Autoenhance order UUID                                  |
| `format`   | query  | `jpeg`  | Output format: `jpeg`, `png`, `webp`, `avif`, `jxl`    |
| `quality`  | query  | —       | Image quality 1-90 (omit for API default)               |
| `preview`  | query  | `true`  | `true` = free preview quality; `false` = full (credits) |
| `dev_mode` | query  | `false` | Test without consuming credits (watermarked output)     |

**Response headers:** `X-Total-Images`, `X-Downloaded`, `X-Failed`

```bash
# Download preview images as a ZIP
curl "https://autoenhance.onrender.com/orders/{order_id}/images?dev_mode=true" -o images.zip
```

## How It Works

1. Validates the order ID (UUID format) — rejects bad input with 400 before any upstream call.
2. Retrieves the order from Autoenhance API v3.
3. Downloads all enhanced images **concurrently** (semaphore, max 5) — each streams into the ZIP as it completes.
4. Automatically retries transient failures (5xx / timeout) once with 1s backoff.
5. Bundles successful downloads into a ZIP. If some images fail, includes a `_download_report.txt`.
6. Returns the ZIP with download stats in response headers.

**Partial failure strategy:** If some images fail (e.g. still processing), the ZIP contains everything that succeeded plus a `_download_report.txt` listing each failure. Recovery is simple — retry the batch (already-processed images download instantly) or fetch individual images via `GET /v3/images/{image_id}/enhanced` using the IDs from the report. A 422 is only returned when *all* images fail.

## Quick Start

```bash
# Clone and install
git clone https://github.com/azoni/autoenhance.git
cd autoenhance
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env — add your Autoenhance API key

# Run
uvicorn app:app --reload
# Open http://localhost:8000
```

## Testing

```bash
pytest test_app.py -v
```

20 tests covering input validation, successful downloads, partial/total failure, retry logic, network errors, upstream error handling, edge cases, health check, and UI rendering. Uses `httpx.MockTransport` — no real API calls, no credits consumed.

## Design Decisions

| Decision | Rationale |
|----------|-----------|
| **ZIP format** | Universal support, no client dependencies. Multipart has poor client support; base64 JSON adds 33% overhead. |
| **Streaming downloads** | `asyncio.as_completed` with semaphore (5). Each image writes to the ZIP and frees immediately — peak memory bounded by concurrency, not order size. |
| **Partial failure + report** | Returns what succeeded + `_download_report.txt` with failed image IDs and recovery instructions. Only 422 if *all* fail. |
| **Retry with backoff** | 1 automatic retry on 5xx/timeout with 1s backoff. No retry on 4xx (client errors). |
| **SpooledTemporaryFile** | ZIPs under 10 MB stay in memory; larger ones spill to disk. Prevents OOM on big orders. |
| **60s per-image timeout** | Generous enough for large images; prevents indefinite hangs. |
| **Follow redirects** | Autoenhance returns 302 → asset server → S3. Handled transparently by httpx. |
| **UUID validation** | Regex check before any upstream call. Rejects SQL injection, malformed input. |
| **Thread-safe stats** | `asyncio.Lock` around all counter mutations prevents race conditions under concurrent requests. |
| **Admin auth** | Httponly cookie (`hmac.compare_digest`) protects credit-consuming endpoints. Not visible in page source. |

## Project Structure

```
autoenhance-batch/
├── app/
│   ├── __init__.py         # FastAPI app, middleware, lifespan
│   ├── config.py           # Constants, limits, format maps
│   ├── auth.py             # Admin token, require_admin()
│   ├── state.py            # Shared HTTP client, stats, locks
│   └── routes/
│       ├── batch.py        # GET /orders/{id}/images (core deliverable)
│       ├── orders.py       # POST create-order endpoints
│       ├── monitoring.py   # /health, /api/stats, Sentry
│       └── ui.py           # Web UI, favicon
├── test_app.py             # 20 unit tests (httpx MockTransport)
├── setup_test_order.py     # CLI helper to create test orders
├── requirements.txt        # Pinned dependencies
├── render.yaml             # Render deployment config
├── sample_images/          # 3 bundled photos for zero-friction demo
├── .env.example            # Environment variable template
├── ENGINEERING_LOG.md      # Detailed build log with decisions and trade-offs
└── README.md
```

## Configuration

```bash
# Required
AUTOENHANCE_API_KEY=your_api_key_here

# Optional — Sentry error tracking
SENTRY_DSN=your_sentry_dsn
SENTRY_AUTH_TOKEN=your_sentry_auth_token
SENTRY_ORG=your_sentry_org
SENTRY_PROJECT=your_sentry_project
```

## Additional Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | Web UI for testing the batch endpoint |
| `GET /health` | Health check (`{"status": "ok", "api_key_configured": true}`) |
| `GET /docs` | Interactive OpenAPI documentation (Swagger UI) |
| `POST /api/create-order` | Upload images to create a test order |
| `POST /api/create-sample-order` | Create an order using bundled sample images |
