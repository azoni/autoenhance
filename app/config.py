"""
Constants and configuration shared across the application.
"""

import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

API_BASE = "https://api.autoenhance.ai/v3"

MAX_CONCURRENT_DOWNLOADS = 5
MAX_IMAGES_PER_ORDER = 100
MAX_UPLOAD_SIZE_BYTES = 20 * 1024 * 1024  # 20 MB per file
MAX_ZIP_SIZE_BYTES = 500 * 1024 * 1024    # 500 MB total ZIP cap
ZIP_CACHE_TTL_SECONDS = 3600              # 1 hour TTL for ZIP cache and job store

# Pre-compiled UUID pattern — validated once at import, not per-request
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

CONTENT_TYPE_MAP = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

# Map output format param to file extension
EXT_MAP = {
    "jpeg": "jpg",
    "png": "png",
    "webp": "webp",
    "avif": "avif",
    "jxl": "jxl",
}

SAMPLE_IMAGES_DIR = BASE_DIR / "sample_images"
