"""Write back to Shopify. For now: image alt text via REST Admin API."""

from __future__ import annotations

import logging

import httpx
from sqlalchemy.orm import Session

from gglads.services import integrations as integrations_svc

logger = logging.getLogger("gglads.shopify_push")


def _normalize_domain(domain: str) -> str:
    d = domain.strip().rstrip("/").replace("https://", "").replace("http://", "")
    if not d.endswith(".myshopify.com") and "." not in d:
        d = f"{d}.myshopify.com"
    return d


def _shopify_request(
    db: Session, method: str, path: str, json_body: dict | None = None
) -> tuple[bool, dict | str]:
    cfg = integrations_svc.get_config(db, "shopify")
    domain = _normalize_domain(cfg.get("store_domain", ""))
    token = (cfg.get("admin_api_token") or "").strip()
    version = (cfg.get("api_version") or "2025-01").strip()
    if not domain or not token:
        return False, "Shopify is not configured."
    url = f"https://{domain}/admin/api/{version}/{path.lstrip('/')}"
    try:
        r = httpx.request(
            method,
            url,
            json=json_body,
            headers={
                "X-Shopify-Access-Token": token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if r.status_code >= 400:
        return False, f"HTTP {r.status_code}: {r.text[:300]}"
    try:
        return True, r.json()
    except ValueError:
        return True, {}


def update_image_alt(
    db: Session, product_id: int, image_id: int, alt_text: str
) -> tuple[bool, str]:
    ok, body = _shopify_request(
        db,
        "PUT",
        f"products/{product_id}/images/{image_id}.json",
        json_body={"image": {"id": image_id, "alt": alt_text or ""}},
    )
    if not ok:
        return False, str(body)
    return True, "Image alt updated in Shopify."
