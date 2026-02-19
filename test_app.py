"""
Unit tests for the batch image download endpoint.

Uses httpx mock transport to simulate Autoenhance API responses
without making real API calls.
"""

import io
import os
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

os.environ["AUTOENHANCE_API_KEY"] = "test-key-for-unit-tests"

from app import app

client = TestClient(app)

VALID_ORDER_ID = "100aefc4-8664-4180-9a97-42f428c6aace"
FAKE_IMAGE_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_mock_client(order_response=None, image_responses=None, order_status=200):
    """Return a patched AsyncClient class that uses a mock transport."""

    if order_response is None:
        order_response = {
            "order_id": VALID_ORDER_ID,
            "name": "Test Order",
            "images": [
                {"image_id": "img-1", "image_name": "house_front"},
                {"image_id": "img-2", "image_name": "house_back"},
            ],
        }

    if image_responses is None:
        image_responses = {
            "img-1": (200, FAKE_IMAGE_BYTES),
            "img-2": (200, FAKE_IMAGE_BYTES),
        }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)

        if "/orders/" in url and "/images" not in url.split("/orders/")[1]:
            return httpx.Response(order_status, json=order_response)

        for img_id, (status, content) in image_responses.items():
            if f"/images/{img_id}/enhanced" in url:
                if status == 200:
                    return httpx.Response(status, content=content)
                return httpx.Response(status, text=f"Error {status}")

        return httpx.Response(404, json={"detail": "Not found"})

    transport = httpx.MockTransport(handler)

    _OriginalAsyncClient = httpx.AsyncClient

    class MockedAsyncClient(_OriginalAsyncClient):
        def __init__(self, **kwargs):
            kwargs.pop("timeout", None)
            kwargs.pop("follow_redirects", None)
            super().__init__(transport=transport, **kwargs)

    return MockedAsyncClient


# ---------------------------------------------------------------------------
# Tests — Input validation
# ---------------------------------------------------------------------------

class TestInputValidation:
    def test_invalid_order_id_format(self):
        resp = client.get("/orders/not-a-uuid/images")
        assert resp.status_code == 400
        assert "Invalid order ID" in resp.json()["detail"]

    def test_sql_injection_rejected(self):
        resp = client.get("/orders/'; DROP TABLE orders;--/images")
        assert resp.status_code == 400

    def test_empty_order_id(self):
        resp = client.get("/orders/%20%20%20/images")
        assert resp.status_code == 400

    def test_valid_uuid_accepted(self):
        # Won't be 400 — proves UUID validation passed
        resp = client.get(f"/orders/{VALID_ORDER_ID}/images?dev_mode=true")
        assert resp.status_code != 400


# ---------------------------------------------------------------------------
# Tests — Successful downloads
# ---------------------------------------------------------------------------

class TestSuccessfulDownload:
    def test_full_success_returns_zip(self, monkeypatch):
        monkeypatch.setattr(httpx, "AsyncClient", make_mock_client())

        resp = client.get(f"/orders/{VALID_ORDER_ID}/images?dev_mode=true&format=jpeg")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"
        assert resp.headers["x-total-images"] == "2"
        assert resp.headers["x-downloaded"] == "2"
        assert resp.headers["x-failed"] == "0"

        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        names = zf.namelist()
        assert len(names) == 2
        assert all(n.endswith(".jpg") for n in names)

    def test_zip_contains_correct_filenames(self, monkeypatch):
        monkeypatch.setattr(httpx, "AsyncClient", make_mock_client())

        resp = client.get(f"/orders/{VALID_ORDER_ID}/images?format=png")
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        names = sorted(zf.namelist())
        assert "house_back.png" in names
        assert "house_front.png" in names


# ---------------------------------------------------------------------------
# Tests — Partial failure
# ---------------------------------------------------------------------------

class TestPartialFailure:
    def test_partial_failure_includes_report(self, monkeypatch):
        monkeypatch.setattr(httpx, "AsyncClient", make_mock_client(
            image_responses={
                "img-1": (200, FAKE_IMAGE_BYTES),
                "img-2": (500, b""),
            }
        ))

        resp = client.get(f"/orders/{VALID_ORDER_ID}/images")
        assert resp.status_code == 200
        assert resp.headers["x-downloaded"] == "1"
        assert resp.headers["x-failed"] == "1"

        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        assert "_download_report.txt" in zf.namelist()
        report = zf.read("_download_report.txt").decode()
        assert "img-2" in report

    def test_all_fail_returns_422(self, monkeypatch):
        monkeypatch.setattr(httpx, "AsyncClient", make_mock_client(
            image_responses={
                "img-1": (500, b""),
                "img-2": (500, b""),
            }
        ))

        resp = client.get(f"/orders/{VALID_ORDER_ID}/images")
        assert resp.status_code == 422
        body = resp.json()
        assert "No images could be downloaded" in body["detail"]["message"]


# ---------------------------------------------------------------------------
# Tests — Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_order_returns_404(self, monkeypatch):
        monkeypatch.setattr(httpx, "AsyncClient", make_mock_client(
            order_response={
                "order_id": VALID_ORDER_ID,
                "name": "Empty Order",
                "images": [],
            }
        ))

        resp = client.get(f"/orders/{VALID_ORDER_ID}/images")
        assert resp.status_code == 404
        assert "no images" in resp.json()["detail"].lower()

    def test_order_not_found(self, monkeypatch):
        not_found_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        monkeypatch.setattr(httpx, "AsyncClient", make_mock_client(
            order_status=404,
            order_response={"detail": "Not found"},
        ))

        resp = client.get(f"/orders/{not_found_id}/images")
        assert resp.status_code == 404

    def test_duplicate_image_names_deduplicated(self, monkeypatch):
        monkeypatch.setattr(httpx, "AsyncClient", make_mock_client(
            order_response={
                "order_id": VALID_ORDER_ID,
                "name": "Dupes",
                "images": [
                    {"image_id": "img-1", "image_name": "photo"},
                    {"image_id": "img-2", "image_name": "photo"},
                ],
            }
        ))

        resp = client.get(f"/orders/{VALID_ORDER_ID}/images?format=jpeg")
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        names = zf.namelist()
        assert len(names) == 2
        assert len(set(names)) == 2  # all unique


# ---------------------------------------------------------------------------
# Tests — Health & UI
# ---------------------------------------------------------------------------

class TestHealthAndUI:
    def test_health_endpoint(self):
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["api_key_configured"] is True

    def test_ui_returns_html(self):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "The Endpoint" in resp.text
