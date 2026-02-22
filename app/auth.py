"""
Admin token authentication for protected endpoints.
"""

import hmac
import logging
import os
import secrets

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

# Set ADMIN_TOKEN in env to gate create-order and sentry-debug endpoints.
# If unset, a random token is generated per-boot (logged at startup).
admin_token: str = os.getenv("ADMIN_TOKEN") or secrets.token_urlsafe(32)
if not os.getenv("ADMIN_TOKEN"):
    logger.info("No ADMIN_TOKEN set — generated ephemeral token: …%s", admin_token[-8:])


def require_admin(request: Request) -> None:
    """Raise 403 if the request doesn't carry a valid admin token."""
    token = (
        request.headers.get("X-Admin-Token")
        or request.query_params.get("token")
        or request.cookies.get("_at")
    )
    if not token or not hmac.compare_digest(token, admin_token):
        raise HTTPException(status_code=403, detail="Forbidden — invalid or missing admin token.")


# Set SERVICE_API_KEY in env to require an X-API-Key header on the batch endpoint.
# If unset, the endpoint is open (backward compatible).
_service_key: str | None = os.getenv("SERVICE_API_KEY")


def require_service_key(request: Request) -> None:
    """Raise 401 if SERVICE_API_KEY is configured and the request doesn't carry a valid key."""
    if not _service_key:
        return  # not configured → endpoint is open
    token = (
        request.headers.get("X-API-Key")
        or request.query_params.get("api_key")
    )
    if not token or not hmac.compare_digest(token, _service_key):
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")
