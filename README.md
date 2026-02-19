# Autoenhance Batch Image Downloader

A FastAPI service that downloads all enhanced images for a given Autoenhance order and returns them as a ZIP archive.

**Live:** https://autoenhance.onrender.com
**API Docs:** https://autoenhance.onrender.com/docs

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
3. Downloads all enhanced images **concurrently** (`asyncio.gather` + semaphore, max 5).
4. Bundles successful downloads into a ZIP. If some images fail, includes a `_download_report.txt`.
5. Returns the ZIP with download stats in response headers.

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

13 tests covering input validation, successful downloads, partial/total failure, edge cases, health check, and UI rendering. Uses `httpx.MockTransport` — no real API calls, no credits consumed.

## Design Decisions

| Decision | Rationale |
|----------|-----------|
| **ZIP format** | Universal support, good compression. Multipart has poor client support; base64 JSON adds 33% overhead. |
| **Concurrent downloads** | `asyncio.gather` with semaphore (5) balances speed vs API rate limits. |
| **Partial failure** | Returns what succeeded + failure report, rather than all-or-nothing. Only 422 if *all* fail. |
| **60s per-image timeout** | Generous enough for large images; prevents indefinite hangs. |
| **Follow redirects** | Autoenhance returns 302 → asset server → S3. Handled transparently by httpx. |
| **In-memory buffering** | Fine for typical orders (<50 images). Would need streaming for larger. |
| **UUID validation** | Regex check before any upstream call. Rejects SQL injection, malformed input. |

## Project Structure

```
autoenhance-batch/
├── app.py                  # FastAPI app — endpoint, UI, create-order helpers
├── test_app.py             # 13 unit tests (httpx MockTransport)
├── setup_test_order.py     # CLI helper to create test orders
├── requirements.txt        # Pinned dependencies
├── render.yaml             # Render deployment config
├── sample_images/          # 3 bundled photos for zero-friction demo
├── favicon.ico             # Autoenhance-styled favicon
├── .env.example            # Environment variable template
├── .gitignore
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
