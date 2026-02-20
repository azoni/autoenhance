"""
Autoenhance Batch Image Downloader

A FastAPI service providing a batch endpoint to download all enhanced images
for a given Autoenhance order as a ZIP archive.

Endpoints:
    GET /orders/{order_id}/images  - Download all images for an order as a ZIP
    GET /health                    - Health check
"""

import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import sentry_sdk
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

# Load .env before any app modules that read os.getenv() at import time (e.g. auth.py)
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from app.config import BASE_DIR  # noqa: E402
from app import state  # noqa: E402
from app.routes import batch, monitoring, orders, ui  # noqa: E402

# Sentry error tracking — only active when DSN is configured
if os.getenv("SENTRY_DSN"):
    sentry_sdk.init(
        dsn=os.getenv("SENTRY_DSN"),
        traces_sample_rate=0.2,
        environment=os.getenv("SENTRY_ENV", "production"),
    )

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

if not os.getenv("AUTOENHANCE_API_KEY"):
    logger.warning("AUTOENHANCE_API_KEY is not set — API requests will fail")


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None]:
    state._http_client = httpx.AsyncClient(timeout=60.0, follow_redirects=True)
    yield
    await state._http_client.aclose()
    state._http_client = None


app = FastAPI(
    title="Autoenhance Batch Image Downloader",
    description=(
        "Batch endpoint that retrieves all enhanced images for a given order "
        "and returns them as a ZIP archive."
    ),
    version="1.0.0",
    lifespan=_lifespan,
)

# ── CORS ──────────────────────────────────────────────────────────────────
_ALLOWED_ORIGINS = [
    "https://autoenhance.onrender.com",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "HEAD", "OPTIONS"],
    allow_headers=["Content-Type", "X-Admin-Token"],
)


# ── Security headers ─────────────────────────────────────────────────────
class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        return response

app.add_middleware(_SecurityHeadersMiddleware)

# ── Include routers ──────────────────────────────────────────────────────
app.include_router(batch.router)
app.include_router(orders.router)
app.include_router(monitoring.router)
app.include_router(ui.router)
