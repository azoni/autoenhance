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
