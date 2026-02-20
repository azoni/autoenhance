"""
Health checks, runtime stats, and Sentry integration endpoints.

GET /health
GET /api/stats
GET /sentry-debug
GET /api/sentry/issues
"""

import hmac
import logging
import os

import httpx
from fastapi import APIRouter, Request

from app.auth import admin_token, require_admin
from app.config import MAX_CONCURRENT_DOWNLOADS, MAX_IMAGES_PER_ORDER
from app.state import stats, get_http_client

logger = logging.getLogger(__name__)

router = APIRouter()


@router.api_route("/health", methods=["GET", "HEAD"])
async def health_check():
    """Health check — also indicates whether the API key is configured."""
    return {
        "status": "ok",
        "api_key_configured": bool(os.getenv("AUTOENHANCE_API_KEY")),
    }


@router.get("/api/stats", include_in_schema=False)
async def runtime_stats(request: Request):
    """Runtime stats for the chatbot and monitoring.

    Returns full stats (including recent_errors) to authenticated admins.
    Unauthenticated requests get counters only — no error details.
    """
    token = (
        request.headers.get("X-Admin-Token")
        or request.query_params.get("token")
        or request.cookies.get("_at")
    )
    is_admin = bool(token) and hmac.compare_digest(token, admin_token)

    result = stats.snapshot(include_errors=is_admin)
    result["limits"] = {
        "max_concurrent_downloads": MAX_CONCURRENT_DOWNLOADS,
        "max_images_per_order": MAX_IMAGES_PER_ORDER,
    }
    return result


@router.get("/sentry-debug", include_in_schema=False)
async def trigger_error(request: Request):
    """Trigger a test error to verify Sentry integration."""
    require_admin(request)
    1 / 0


@router.get("/api/sentry/issues", include_in_schema=False)
async def sentry_issues():
    """Proxy to Sentry API — returns recent issues for the project."""
    token = os.getenv("SENTRY_AUTH_TOKEN")
    org = os.getenv("SENTRY_ORG")
    project = os.getenv("SENTRY_PROJECT")
    if not all([token, org, project]):
        return {"issues": [], "error": "Sentry API not configured"}

    client = get_http_client()
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
    except httpx.HTTPError as e:
        logger.warning("Sentry API request failed: %s", e)
        return {"issues": [], "error": "Failed to reach Sentry API"}
