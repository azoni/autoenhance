"""
Setup Test Order

Helper script to create a test order on Autoenhance.ai by uploading local images.
Use this to generate test data for the batch download endpoint.

Usage:
    python setup_test_order.py image1.jpg image2.jpg image3.jpg

The script will:
    1. Create a new order on Autoenhance.
    2. Register and upload each image to that order.
    3. Print the order_id for use with the batch endpoint.

Images typically take 30-60 seconds to process after upload.
"""

import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

API_BASE = "https://api.autoenhance.ai/v3"
API_KEY = os.getenv("AUTOENHANCE_API_KEY")

CONTENT_TYPE_MAP = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def create_order(name: str) -> str:
    """Create a new order and return its order_id."""
    resp = requests.post(
        f"{API_BASE}/orders",
        headers={"x-api-key": API_KEY, "Content-Type": "application/json"},
        json={"name": name},
    )
    resp.raise_for_status()
    data = resp.json()
    order_id = data["order_id"]
    print(f"Created order '{name}' -> {order_id}")
    return order_id


def register_image(order_id: str, image_name: str, content_type: str) -> dict:
    """Register an image with Autoenhance, returning upload URL and image_id."""
    resp = requests.post(
        f"{API_BASE}/images/",
        headers={"x-api-key": API_KEY, "Content-Type": "application/json"},
        json={
            "image_name": image_name,
            "order_id": order_id,
            "contentType": content_type,
        },
    )
    resp.raise_for_status()
    return resp.json()


def upload_image(upload_url: str, file_path: Path, content_type: str):
    """Upload image binary data to the presigned S3 URL."""
    with open(file_path, "rb") as f:
        resp = requests.put(
            upload_url,
            data=f,
            headers={"Content-Type": content_type},
        )
    resp.raise_for_status()


def main():
    if not API_KEY:
        print("Error: AUTOENHANCE_API_KEY is not set.")
        print("Copy .env.example to .env and add your API key.")
        sys.exit(1)

    if len(sys.argv) < 2:
        print("Usage: python setup_test_order.py <image1.jpg> [image2.jpg ...]")
        print("\nUpload one or more images to create a test order.")
        sys.exit(1)

    image_paths = [Path(p) for p in sys.argv[1:]]

    # Validate all files before starting
    for path in image_paths:
        if not path.exists():
            print(f"Error: File not found: {path}")
            sys.exit(1)
        if path.suffix.lower() not in CONTENT_TYPE_MAP:
            print(f"Error: Unsupported format '{path.suffix}'. Use: .jpg, .jpeg, .png, .webp")
            sys.exit(1)

    # 1. Create the order
    order_id = create_order(f"Test Order ({len(image_paths)} images)")

    # 2. Register and upload each image
    for path in image_paths:
        content_type = CONTENT_TYPE_MAP[path.suffix.lower()]
        print(f"\nRegistering {path.name}...")

        data = register_image(order_id, path.stem, content_type)
        upload_url = data.get("s3PutObjectUrl") or data.get("upload_url")
        image_id = data.get("image_id")
        print(f"  Image ID: {image_id}")

        print(f"  Uploading...")
        upload_image(upload_url, path, content_type)
        print(f"  Done.")

    # 3. Print summary
    print("\n" + "=" * 50)
    print(f"Order ID: {order_id}")
    print(f"Images uploaded: {len(image_paths)}")
    print("=" * 50)
    print(f"\nWait ~60 seconds for processing, then test with:")
    print(f"  curl http://localhost:8000/orders/{order_id}/images?dev_mode=true -o images.zip")


if __name__ == "__main__":
    main()
