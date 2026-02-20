# Engineering Log

## Project: Autoenhance Batch Image Downloader

Technical test — build a batch endpoint to download all images for an order.

**Repo:** https://github.com/azoni/autoenhance
**Live:** https://autoenhance.onrender.com
**API Docs:** https://autoenhance.onrender.com/docs

---

### 1. Initial Build & Deploy

**Task understanding:**
- Accept an `order_id`, retrieve the order, download every enhanced image, return them grouped in one response.
- The spec is intentionally vague on "group and respond appropriately" — we chose a ZIP archive.

**Key decisions:**
- **FastAPI** over Flask — async support for concurrent image downloads, auto-generated Swagger docs at `/docs`.
- **ZIP response** over multipart/base64 JSON — universally supported, compresses well, easy to save.
- **Concurrent downloads** with `asyncio.gather` + semaphore (max 5) — balances speed vs API rate limits.
- **Partial failure handling** — if some images fail (still processing), we still return what we can + a `_download_report.txt` in the ZIP.

**Setup & testing:**
- No test order provided — created one by uploading 3 Unsplash house photos via `setup_test_order.py`.
- Discovered Autoenhance returns 302 redirects (API → asset server → S3) — added `follow_redirects=True`.
- Fixed `.env` loading to use absolute path (script directory) so it works regardless of cwd.

**Deployment:**
- Hosting on **Render** (free tier) — auto-deploys from GitHub repo `azoni/autoenhance`.
- **UptimeRobot** pinging `/health` every 5 min to prevent free-tier sleep.
- `render.yaml` included for one-click deploy config.

---

### 2. Web UI & Batch Endpoint Documentation

**Web UI added (`GET /`):**
- Single-page form served at `/` — enter order ID, pick format/quality, click download.
- Styled to match autoenhance.ai branding (dark blue `#222173`, teal gradient `#3bd8be` → `#77bff6`).
- Eliminates need for curl to demo the endpoint.

**Batch vs standard endpoint — documented on-page:**
- **Latency**: fans out to N upstream calls; mitigated with concurrent downloads (semaphore-limited to 5).
- **Partial failure**: unlike a standard endpoint (all-or-nothing), batch handles partial success with a failure report.
- **Memory**: images buffered in-memory before ZIP creation; acceptable for typical orders (<50 images).
- **Timeouts**: 60s per-image timeout via httpx; for very large orders, an async job pattern would be needed.
- **Redirects**: Autoenhance returns 302 → asset server → S3; `follow_redirects=True` handles this transparently.

**Assumptions surfaced on-page:**
- Downstream of upload pipeline — we don't control when/how images are uploaded.
- Enhancement timing unknown — images may still be processing when called.
- No webhook/callback to signal order completion — caller must know when to call.
- Rate limits undocumented — semaphore (5) is a conservative default.
- Order schema partially documented — code handles field name variations (`image_id`/`id`, `image_name`/`name`).

---

### 3. Hardening: Validation, Tests, Sentry, Production Tab

**UUID input validation:**
- Regex check at the top of the batch endpoint — rejects non-UUID order IDs with 400 before any upstream call.
- Prevents wasted API round-trips and potential injection vectors.

**Unit test suite (`test_app.py` — 20 tests):**
- Covers: input validation (invalid UUID, SQL injection, empty ID, valid UUID passthrough), successful ZIP downloads (content + filenames), partial failure with report, total failure → 422, query param passthrough, retry on 5xx (verifies retry count), no retry on 4xx, network error → 502, upstream 401/502, order too large → 413, empty order → 404, order not found, duplicate filename deduplication, health check, UI rendering.
- Uses `httpx.MockTransport` — no real API calls, no credits consumed.
- Each test creates its own mock client via `monkeypatch` for full isolation.

**Sentry error tracking:**
- `sentry-sdk[fastapi]` — captures unhandled exceptions with request context.
- Activated via `SENTRY_DSN` env var; no-op when unset.
- 20% trace sampling to keep costs low.
- Live error dashboard on the Production tab with "Trigger Test Error" button.

**Tabbed UI:**
- **Interview Submission** tab — the endpoint reference, interactive test UI, design decisions, assumptions, and production considerations.
- **Production Version** tab — code walkthroughs of implemented features (UUID validation, test suite, Sentry) plus annotated examples of next-step patterns (circuit breaker, caching, rate limiting, async job pattern).

---

### 4. Create-Order UI & Sample Images

**In-page order creation:**
- "Need a test order?" collapsible section lets the interviewer upload images directly from the browser.
- POST to `/api/create-order` — registers images with Autoenhance, uploads to their S3, returns the order ID.
- Order ID auto-fills the download form.

**Sample images:**
- Bundled 3 real-estate photos in `sample_images/` so the demo works without any file uploads.
- "Try Demo — Use Sample Images" button calls `/api/create-sample-order` — zero-friction testing.

---

### 5. API Endpoint Section & Chat Tab

**API endpoint reference:**
- Added a prominent card at the top of the Interview tab showing `GET /orders/{order_id}/images` with syntax-highlighted params, a collapsible curl example, and a link to the OpenAPI docs at `/docs`.
- Renamed the main card from "Batch Image Downloader" to "Try It" — clearer separation between documentation and interactive testing.

**"Ask About This Project" chatbot tab:**
- Integrated Charlton's portfolio chatbot (from azoni.ai) as a third tab.
- Calls `POST https://azoni.ai/.netlify/functions/chat` with `context: 'autoenhance-interview'` — the backend injects detailed knowledge about this specific endpoint (design decisions, tech stack, test strategy, production considerations).
- 6 suggested questions as chips: "What does this batch endpoint do?", "What design decisions were made?", "How are errors handled?", "What would you add for production?", "Tell me about Charlton's background", "Why did Charlton build it this way?"
- Cross-origin CORS handled (OPTIONS preflight fixed in chat.js backend).
- Chats log to Firestore with source context (`autoenhance-interview`) so they're distinguishable from regular azoni.ai traffic.

---

### 6. Code Architecture & Production Hardening

**Split `app.py` into a multi-file package (`app/`):**
- Monolithic 660-line `app.py` → 8-file package with clear separation of concerns.
- `app/__init__.py` — app factory, middleware, lifespan, Sentry init.
- `app/config.py` — constants (API_BASE, limits, regex, maps, paths).
- `app/state.py` — shared mutable state (`Stats` class, HTTP client, helpers).
- `app/auth.py` — admin token generation and `require_admin()` guard.
- `app/routes/batch.py` — core deliverable (batch download endpoint).
- `app/routes/orders.py` — order creation endpoints for testing.
- `app/routes/monitoring.py` — health, stats, Sentry integration.
- `app/routes/ui.py` — web UI serving and favicon.
- `uvicorn app:app` still works — Python resolves `app/__init__.py:app`.

**Thread-safe `Stats` class:**
- Replaced raw `_stats` dict + external `asyncio.Lock` with a `Stats` class that encapsulates the lock internally.
- Mutation methods (`record_batch_complete`, `record_batch_total_failure`, `record_order_created`) each acquire the lock automatically.
- `snapshot()` method returns a clean dict for API responses — callers don't touch internals.

**Retry logic with backoff:**
- Image downloads retry once on transient failures (5xx, timeout) with a 1-second backoff.
- Client errors (4xx) are not retried — no point retrying a deterministic failure.
- Tested: mock handler returns 500 on first call, 200 on retry; test asserts attempt count == 2.

**Network error handling:**
- Order retrieval wrapped in `try/except httpx.HTTPError` → returns 502 with clear message.
- Tested: mock transport raises `httpx.ConnectError`; test asserts 502 response.

**Security hardening:**
- Admin token via httponly cookie (not embedded in page source).
- `/api/stats` returns `recent_errors` only to authenticated admins.
- File upload size checked via `file.size` before reading into memory.
- Security headers middleware (nosniff, DENY framing, referrer policy, permissions policy).
- `hmac.compare_digest` for timing-safe token comparison.
- Sentry disabled in test environment via `SENTRY_DSN=""`.

**Streaming memory model:**
- Replaced `asyncio.gather` (buffered all images in memory) with `asyncio.as_completed` — each image is written to the ZIP and freed as soon as it finishes downloading.
- Peak memory is now bounded by the semaphore (max 5 in-flight downloads), not the total order size.
- ZIP uses `SpooledTemporaryFile` — small ZIPs stay in memory; large ones (>10 MB) spill to disk automatically.

---

### Architecture Overview

```
┌──────────────────────────────────────┐
│  Browser (Interview UI)              │
│  ┌──────────┬───────────┬──────────┐ │
│  │Interview │Production │  Chat    │ │
│  │  Tab     │   Tab     │  Tab     │ │
│  └────┬─────┴─────┬─────┴────┬─────┘ │
└───────┼───────────┼──────────┼───────┘
        │           │          │
        ▼           ▼          ▼
   GET /orders/   GET /api/  POST azoni.ai
   {id}/images    sentry/    /.netlify/
        │         issues     functions/chat
        │           │          │
        ▼           ▼          ▼
   Autoenhance    Sentry     OpenRouter
   API (v3)       API        (GPT-4o-mini)
```

**Files:**
| File | Purpose |
|------|---------|
| `app/__init__.py` | FastAPI app factory, middleware, lifespan, Sentry init |
| `app/config.py` | Constants: API base URL, limits, regex, content-type maps |
| `app/state.py` | `Stats` class, HTTP client singleton, API key helper |
| `app/auth.py` | Admin token generation and `require_admin()` guard |
| `app/routes/batch.py` | Core deliverable — `GET /orders/{id}/images` |
| `app/routes/orders.py` | `POST /api/create-order`, `POST /api/create-sample-order` |
| `app/routes/monitoring.py` | `/health`, `/api/stats`, `/sentry-debug`, Sentry proxy |
| `app/routes/ui.py` | `GET /` (web UI), `GET /favicon.ico` |
| `test_app.py` | 20 unit tests with httpx MockTransport |
| `setup_test_order.py` | CLI helper to create test orders by uploading local images |
| `requirements.txt` | Pinned Python dependencies |
| `render.yaml` | Render deployment config |
| `sample_images/` | 3 bundled real-estate photos for zero-friction demo |
| `.env.example` | Environment variable template |
