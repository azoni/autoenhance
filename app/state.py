"""
Shared mutable state: HTTP client, runtime stats, and helpers.
"""

import asyncio
import os
import time

import httpx
from fastapi import HTTPException

# Shared HTTP client — created once at startup, reuses connections across requests
_http_client: httpx.AsyncClient | None = None


class Stats:
    """Thread-safe runtime statistics. All mutations go through async methods that hold the lock."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.started_at = time.time()
        self.orders_processed = 0
        self.images_downloaded = 0
        self.images_failed = 0
        self.zips_served = 0
        self.orders_created = 0
        self.images_uploaded = 0
        self._errors: list[dict] = []

    async def record_batch_complete(
        self, *, downloaded: int, failed: int, order_id: str, total: int
    ) -> None:
        """Record a successful batch download (ZIP served, possibly with partial failures)."""
        async with self._lock:
            self.orders_processed += 1
            self.images_downloaded += downloaded
            self.images_failed += failed
            self.zips_served += 1
            if failed:
                self._errors.append({
                    "time": time.time(),
                    "order_id": order_id,
                    "error": f"Partial failure: {failed}/{total} images failed",
                    "count": failed,
                })
                self._errors = self._errors[-20:]

    async def record_batch_total_failure(
        self, *, failed: int, order_id: str
    ) -> None:
        """Record that all images in a batch failed (no ZIP served)."""
        async with self._lock:
            self.orders_processed += 1
            self.images_failed += failed
            self._errors.append({
                "time": time.time(),
                "order_id": order_id,
                "error": "All images failed",
                "count": failed,
            })
            self._errors = self._errors[-20:]

    async def record_order_created(self, *, images_uploaded: int) -> None:
        """Record creation of a new order with uploaded images."""
        async with self._lock:
            self.orders_created += 1
            self.images_uploaded += images_uploaded

    def snapshot(self, *, include_errors: bool = False) -> dict:
        """Return current counters as a plain dict for API responses."""
        result = {
            "uptime_seconds": round(time.time() - self.started_at),
            "orders_processed": self.orders_processed,
            "images_downloaded": self.images_downloaded,
            "images_failed": self.images_failed,
            "zips_served": self.zips_served,
            "orders_created": self.orders_created,
            "images_uploaded": self.images_uploaded,
        }
        if include_errors:
            result["recent_errors"] = self._errors[-5:]
        return result


stats = Stats()


def get_http_client() -> httpx.AsyncClient:
    assert _http_client is not None, "HTTP client not initialised"
    return _http_client


def get_api_key() -> str:
    key = os.getenv("AUTOENHANCE_API_KEY")
    if not key:
        raise HTTPException(
            status_code=500,
            detail="AUTOENHANCE_API_KEY environment variable is not set.",
        )
    return key
