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


_PRODUCT_UPDATE_MUTATION = """
mutation productUpdate($input: ProductInput!) {
  productUpdate(input: $input) {
    product { id }
    userErrors { field message }
  }
}
"""


def update_product_seo(
    db: Session,
    product_id: int,
    *,
    title: str | None = None,
    description_html: str | None = None,
    seo_title: str | None = None,
    seo_description: str | None = None,
) -> tuple[bool, str]:
    """Push product SEO fields back to Shopify via GraphQL productUpdate."""
    cfg = integrations_svc.get_config(db, "shopify")
    domain = _normalize_domain(cfg.get("store_domain", ""))
    token = (cfg.get("admin_api_token") or "").strip()
    version = (cfg.get("api_version") or "2025-01").strip()
    if not domain or not token:
        return False, "Shopify is not configured."

    input_data: dict = {"id": f"gid://shopify/Product/{product_id}"}
    if title is not None:
        input_data["title"] = title
    if description_html is not None:
        input_data["descriptionHtml"] = description_html
    seo_obj = {}
    if seo_title is not None:
        seo_obj["title"] = seo_title
    if seo_description is not None:
        seo_obj["description"] = seo_description
    if seo_obj:
        input_data["seo"] = seo_obj

    if len(input_data) == 1:
        return False, "Nothing to update."

    url = f"https://{domain}/admin/api/{version}/graphql.json"
    try:
        r = httpx.post(
            url,
            json={
                "query": _PRODUCT_UPDATE_MUTATION,
                "variables": {"input": input_data},
            },
            headers={
                "X-Shopify-Access-Token": token,
                "Content-Type": "application/json",
            },
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if r.status_code >= 400:
        return False, f"HTTP {r.status_code}: {r.text[:300]}"
    try:
        data = r.json()
    except ValueError:
        return False, "Non-JSON response from Shopify."
    if data.get("errors"):
        return False, f"GraphQL errors: {data['errors']}"
    user_errors = (
        data.get("data", {}).get("productUpdate", {}).get("userErrors") or []
    )
    if user_errors:
        msgs = "; ".join(
            f"{(e.get('field') or [''])[-1]}: {e.get('message')}" for e in user_errors
        )
        return False, f"Shopify rejected: {msgs}"
    return True, "Updated in Shopify."
