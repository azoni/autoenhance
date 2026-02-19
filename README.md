# Autoenhance Batch Image Downloader

A FastAPI service that provides a batch endpoint to download all enhanced images for a given Autoenhance order as a ZIP archive.

## Overview

This service wraps the [Autoenhance API](https://docs.autoenhance.ai/) to provide a single endpoint that:

1. Retrieves an order by ID (`GET /v3/orders/{order_id}`)
2. Downloads all enhanced images for that order concurrently (`GET /v3/images/{id}/enhanced`)
3. Bundles the images into a ZIP file and returns it

## Setup

### Prerequisites

- Python 3.10+
- An [Autoenhance.ai](https://autoenhance.ai) account with an API key

### Installation

```bash
cd autoenhance-batch
pip install -r requirements.txt
```

### Configuration

```bash
cp .env.example .env
```

Edit `.env` and add your API key (found at https://app.autoenhance.ai/settings):

```
AUTOENHANCE_API_KEY=your_api_key_here
```

### Creating a Test Order

If you don't have an existing order with enhanced images, use the helper script to upload some:

```bash
python setup_test_order.py photo1.jpg photo2.jpg photo3.jpg
```

This will create an order, upload your images for enhancement, and print the `order_id`. Wait roughly 60 seconds for Autoenhance to process the images before testing the batch endpoint.

## Running the Server

```bash
uvicorn app:app --reload
```

The server starts at http://localhost:8000.
Interactive API docs (Swagger UI) are available at http://localhost:8000/docs.

## API Reference

### `GET /orders/{order_id}/images`

Download all enhanced images for an order as a ZIP archive.

**Path Parameters:**

| Parameter  | Type   | Description           |
| ---------- | ------ | --------------------- |
| `order_id` | string | The Autoenhance order ID |

**Query Parameters:**

| Parameter  | Type   | Default  | Description                                              |
| ---------- | ------ | -------- | -------------------------------------------------------- |
| `preview`  | bool   | `true`   | `true` = free preview quality; `false` = full quality (uses credits) |
| `format`   | string | `jpeg`   | Output format: `jpeg`, `png`, `webp`, `avif`, `jxl`     |
| `quality`  | int    | —        | Image quality 1–90 (omit for API default)                |
| `dev_mode` | bool   | `false`  | Test without consuming credits (images are watermarked)  |

**Responses:**

| Status | Description |
| ------ | ----------- |
| 200    | ZIP archive containing the enhanced images |
| 404    | Order not found or contains no images |
| 401    | Invalid API key |
| 422    | No images could be downloaded (e.g. all still processing) |

Response headers `X-Total-Images`, `X-Downloaded`, and `X-Failed` report image counts.

**Examples:**

```bash
# Download preview images (free, default)
curl http://localhost:8000/orders/YOUR_ORDER_ID/images -o images.zip

# Full-quality PNGs via dev mode (watermarked, no credits used)
curl "http://localhost:8000/orders/YOUR_ORDER_ID/images?preview=false&format=png&dev_mode=true" -o images.zip
```

### `GET /health`

Returns `{"status": "ok", "api_key_configured": true/false}`.

## Design Decisions

### Why FastAPI?

- **Async support** — ideal for making many concurrent HTTP calls to the Autoenhance API.
- **Automatic OpenAPI docs** — the `/docs` page provides a ready-made interactive UI for testing.
- **Built-in validation** — query parameters are validated (format enum, quality range) with clear error messages.

### Why a ZIP archive?

A ZIP is the most practical way to return multiple binary files in a single HTTP response:

- Universally supported by all operating systems and programming languages.
- Supports compression to reduce transfer size.
- Alternatives considered and rejected:
  - **Multipart response** — poor client support, harder to save to disk.
  - **Base64 JSON** — ~33% size overhead for encoding binary data.
  - **Individual URLs** — would require the caller to make N additional requests.

### Concurrency Strategy

Images are downloaded concurrently using `asyncio.gather` with a semaphore (limit of 5) to balance throughput against API rate limits. Each download has a 60-second timeout.

### Partial Failure Handling

If some images fail to download (e.g. still being processed), the endpoint still returns successfully with the images that *did* download. A `_download_report.txt` file is included in the ZIP documenting which images failed and why. If *all* images fail, a 422 error is returned with structured details.

### Image Field Name Resilience

The order response schema isn't fully documented, so the code checks for both `image_id`/`id` and `image_name`/`name` field variants to be resilient to schema variations.

## Assumptions

1. The `images` array in the order response contains objects with at least an `image_id` (or `id`) field.
2. Images that haven't finished processing will return a non-200 status from the download endpoint.
3. The Autoenhance API uses `x-api-key` header authentication for all endpoints.
4. File naming uses the `image_name` from Autoenhance, with automatic deduplication for collisions.
5. Preview mode is the default to avoid accidentally consuming credits during testing.

## Project Structure

```
autoenhance-batch/
├── app.py                 # FastAPI application with the batch endpoint
├── setup_test_order.py    # Helper to create test orders by uploading images
├── requirements.txt       # Python dependencies
├── .env.example           # Environment variable template
└── README.md              # This file
```
