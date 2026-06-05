"""S3-compatible object storage for generated images and email assets.

Uploads bytes and returns a public URL. boto3 is optional — if it (or the
S3_* config) is missing we return a clear error rather than crashing, so the
rest of Helena degrades gracefully in environments without storage wired up.
"""

from __future__ import annotations

import logging
import uuid

from gglads.config import get_settings

logger = logging.getLogger("gglads.helena.storage")


def is_configured() -> bool:
    s = get_settings()
    return bool(s.s3_bucket and s.s3_access_key_id and s.s3_secret_access_key)


def put_bytes(
    data: bytes,
    *,
    content_type: str = "image/png",
    key_prefix: str = "helena",
    ext: str = "png",
) -> tuple[str | None, str | None]:
    """Store bytes, return (public_url, error)."""
    if not is_configured():
        return None, "S3 storage is not configured (set S3_* env vars)."
    try:
        import boto3  # type: ignore
    except ImportError:
        return None, "boto3 is not installed; cannot upload to S3."

    s = get_settings()
    key = f"{key_prefix}/{uuid.uuid4().hex}.{ext}"
    try:
        client = boto3.client(
            "s3",
            endpoint_url=s.s3_endpoint_url or None,
            region_name=s.s3_region or None,
            aws_access_key_id=s.s3_access_key_id,
            aws_secret_access_key=s.s3_secret_access_key,
        )
        client.put_object(
            Bucket=s.s3_bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
    except Exception as exc:
        return None, f"S3 upload failed: {type(exc).__name__}: {exc}"

    if s.s3_public_base_url:
        return f"{s.s3_public_base_url.rstrip('/')}/{key}", None
    if s.s3_endpoint_url:
        return f"{s.s3_endpoint_url.rstrip('/')}/{s.s3_bucket}/{key}", None
    return f"https://{s.s3_bucket}.s3.{s.s3_region}.amazonaws.com/{key}", None
