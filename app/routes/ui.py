"""
Web UI and static file serving.

GET /
GET /favicon.ico
"""

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse

from app.auth import admin_token
from app.config import BASE_DIR

router = APIRouter()

_BASE_HTML: str = (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")


@router.get("/", response_class=HTMLResponse)
async def ui():
    """Web UI for testing the batch download endpoint — served from static/index.html."""
    response = HTMLResponse(_BASE_HTML)
    # Set admin token as httponly cookie — not visible in page source or JS
    response.set_cookie(
        "_at",
        admin_token,
        httponly=True,
        samesite="strict",
        secure=False,  # allow localhost; Render upgrades to HTTPS automatically
    )
    return response


@router.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(BASE_DIR / "favicon.ico", media_type="image/x-icon")
