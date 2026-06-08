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

import httpx

from gglads.config import get_settings

logger = logging.getLogger("gglads.helena.storage")


def verify_url(url: str, timeout: float = 15.0) -> tuple[bool, str]:
    """Check that a URL is publicly reachable (anonymous, no auth), so we never
    hand the user a link that 404s/403s. Returns (ok, detail)."""
    if not url:
        return False, "empty url"
    try:
        r = httpx.head(url, timeout=timeout, follow_redirects=True)
        if r.status_code in (405, 403, 501):  # HEAD not allowed → try a ranged GET
            r = httpx.get(url, headers={"Range": "bytes=0-0"}, timeout=timeout,
                          follow_redirects=True)
    except httpx.HTTPError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if r.status_code in (200, 206):
        return True, "ok"
    return False, f"HTTP {r.status_code}"


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


def _public_url(c: dict[str, str], key: str) -> str:
    if c["public_base"]:
        return f"{c['public_base'].rstrip('/')}/{key}"
    if c["endpoint"]:
        return f"{c['endpoint'].rstrip('/')}/{c['bucket']}/{key}"
    return f"https://{c['bucket']}.s3.{c['region']}.amazonaws.com/{key}"


def put_bytes(
    data: bytes,
    *,
    content_type: str = "image/png",
    key_prefix: str = "helena",
    ext: str = "png",
    verify: bool = True,
) -> tuple[str | None, str | None]:
    """Store bytes and return (public_url, error).

    When verify=True (default) the returned URL is confirmed publicly reachable
    before we hand it back, so callers never surface a dead/private link. If the
    object stored but isn't reachable (usually a missing S3_PUBLIC_BASE_URL),
    we return an error explaining how to fix it instead of a broken URL.
    """
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

    url = _public_url(c, key)
    if verify:
        ok, detail = verify_url(url)
        if not ok:
            logger.warning("stored object not publicly reachable: %s (%s)", url, detail)
            return None, (
                f"Image saved but isn't publicly reachable ({detail}). Set "
                "S3_PUBLIC_BASE_URL to your public bucket/CDN URL (e.g. the R2 "
                "public dev URL) so generated media can be displayed."
            )
    return url, None


def key_from_url(url: str) -> str | None:
    """Best-effort: recover the object key from a public/endpoint URL so we can
    delete it. Keys are 'helena/<prefix>/<id>.<ext>'."""
    if not url:
        return None
    marker = "helena/"
    idx = url.find(marker)
    return url[idx:] if idx != -1 else None


def delete_url(url: str) -> tuple[bool, str | None]:
    """Delete the stored object behind a public URL. Returns (ok, error).
    A missing key or unconfigured storage is treated as a no-op success so the
    DB record can still be removed."""
    key = key_from_url(url)
    if not key or not is_configured():
        return True, None
    try:
        import boto3  # type: ignore
    except ImportError:
        return True, None
    c = _resolve()
    try:
        client = boto3.client(
            "s3",
            endpoint_url=c["endpoint"] or None,
            region_name=c["region"] or None,
            aws_access_key_id=c["access_key"],
            aws_secret_access_key=c["secret_key"],
        )
        client.delete_object(Bucket=c["bucket"], Key=key)
    except Exception as exc:
        return False, f"Object-storage delete failed: {type(exc).__name__}: {exc}"
    return True, None
