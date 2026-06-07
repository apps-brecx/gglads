"""S3-compatible object storage for generated images and email assets.

Works with AWS S3, Cloudflare R2, or GCS (S3 API) — set an endpoint for the
latter two. Credentials may be given as either S3_* or the conventional AWS_*
names; whichever is present is used. Uploads bytes and returns a public URL.
boto3 is optional — if it or the config is missing we return a clear error
rather than crashing.
"""

from __future__ import annotations

import logging
import uuid

from gglads.config import get_settings

logger = logging.getLogger("gglads.helena.storage")


def _resolve() -> dict[str, str]:
    """Effective storage settings, merging S3_* and AWS_* fallbacks."""
    s = get_settings()
    return {
        "bucket": (s.s3_bucket or "").strip(),
        "endpoint": (s.s3_endpoint_url or "").strip(),
        "region": (s.s3_region or s.aws_region or "us-east-1").strip(),
        "access_key": (s.s3_access_key_id or s.aws_access_key_id or "").strip(),
        "secret_key": (s.s3_secret_access_key or s.aws_secret_access_key or "").strip(),
        "public_base": (s.s3_public_base_url or s.s3_public_url or "").strip(),
    }


def is_configured() -> bool:
    c = _resolve()
    return bool(c["bucket"] and c["access_key"] and c["secret_key"])


def config_error() -> str | None:
    """Human-readable reason storage is unusable, or None if it looks ready."""
    c = _resolve()
    missing = []
    if not c["bucket"]:
        missing.append("S3_BUCKET")
    if not c["access_key"]:
        missing.append("S3_ACCESS_KEY_ID (or AWS_ACCESS_KEY_ID)")
    if not c["secret_key"]:
        missing.append("S3_SECRET_ACCESS_KEY (or AWS_SECRET_ACCESS_KEY)")
    if missing:
        return "Image storage is not configured — set " + ", ".join(missing) + "."
    return None


def put_bytes(
    data: bytes,
    *,
    content_type: str = "image/png",
    key_prefix: str = "helena",
    ext: str = "png",
) -> tuple[str | None, str | None]:
    """Store bytes, return (public_url, error)."""
    err = config_error()
    if err:
        return None, err
    try:
        import boto3  # type: ignore
    except ImportError:
        return None, "boto3 is not installed; cannot upload to object storage."

    c = _resolve()
    key = f"{key_prefix}/{uuid.uuid4().hex}.{ext}"
    try:
        client = boto3.client(
            "s3",
            endpoint_url=c["endpoint"] or None,
            region_name=c["region"] or None,
            aws_access_key_id=c["access_key"],
            aws_secret_access_key=c["secret_key"],
        )
        client.put_object(
            Bucket=c["bucket"],
            Key=key,
            Body=data,
            ContentType=content_type,
        )
    except Exception as exc:
        return None, f"Object-storage upload failed: {type(exc).__name__}: {exc}"

    if c["public_base"]:
        return f"{c['public_base'].rstrip('/')}/{key}", None
    if c["endpoint"]:
        return f"{c['endpoint'].rstrip('/')}/{c['bucket']}/{key}", None
    return f"https://{c['bucket']}.s3.{c['region']}.amazonaws.com/{key}", None
