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
