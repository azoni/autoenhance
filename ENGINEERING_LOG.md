# Engineering Log

## Project: Autoenhance Batch Image Downloader

Technical test — build a batch endpoint to download all images for an order.

---

### 2026-02-19 — Initial Build & Deploy

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
- Order ID: `100aefc4-8664-4180-9a97-42f428c6aace` (3 images).
- Discovered Autoenhance returns 302 redirects (API → asset server → S3) — added `follow_redirects=True`.
- Fixed `.env` loading to use absolute path (script directory) so it works regardless of cwd.

**Deployment:**
- Hosting on **Render** (free tier) — auto-deploys from GitHub repo `azoni/autoenhance`.
- **UptimeRobot** pinging `/health` every 5 min to prevent free-tier sleep.
- `render.yaml` included for one-click deploy config.

**Repo:** https://github.com/azoni/autoenhance

---

### 2026-02-19 — UI & Batch Endpoint Considerations

**Web UI added:**
- Restyled to match autoenhance.ai branding (dark blue #222173, teal gradient #3bd8be → #77bff6, light background).
- Single-page form served at `/` — enter order ID, pick format/quality, click download.
- Eliminates need for curl to demo the endpoint.

**Batch endpoint vs normal endpoint — what we considered:**
- **Latency**: A batch endpoint fans out to N upstream calls. Ours downloads images concurrently (semaphore-limited to 5) to keep total time reasonable.
- **Partial failure**: Unlike a normal endpoint (all-or-nothing), batch must handle partial success. We return whatever images succeed + a failure report.
- **Memory**: All images are buffered in memory before ZIP creation. Acceptable for typical order sizes (<50 images), but for very large orders a streaming ZIP approach would be needed.
- **Timeouts**: Risk scales with image count. We use a 60s httpx timeout per image. For production, a webhook/async pattern (POST to start, poll for result) would be more robust.
- **Rate limiting**: We throttle upstream calls via semaphore to avoid hitting Autoenhance API limits.

**Assumptions surfaced:**
- We're downstream of the upload pipeline — we don't control when or how images are uploaded and enhanced.
- Enhancement timing is unknown — images could still be processing when the batch endpoint is called.
- No webhook/callback exists to tell us when an order is fully processed — caller must know when to call.
- Rate limits are undocumented — our semaphore (5) is a safe guess, could be tuned.
- Order response schema is partially documented — we handle field name variations defensively.

**Possible extensions noted:** poll-until-ready mode, async job pattern for large orders, webhook-triggered downloads.

**Production considerations documented on-page:**
- Observability: have structured logging, would add OpenTelemetry tracing + Sentry.
- Metrics: latency percentiles, success/failure rates, upstream health, ZIP size distribution.
- Versioning: pinned to Autoenhance `/v3`; our own API unversioned — would add `/v1/` prefix.
- Security: API key isolated in env; would add UUID input validation, rate limiting, auth on our endpoint.
- Testing: manual E2E done; would add unit tests (mocked), integration tests (dev-mode), load tests.
- Operational: health check exists; would add caching (images are immutable), circuit breaker, CI/CD pipeline.

Each item marked as "done" (teal) or "next step" (gray) on the live page.

**Live URL:** https://autoenhance.onrender.com
