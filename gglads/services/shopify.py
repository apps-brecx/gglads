"""Shopify Admin API sync.

Pulls the full catalog (collections + products + variants + memberships) into
the DB via the GraphQL Admin API. Pagination handled with cursors.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from gglads.models.shopify_product import (
    ShopifyCollection,
    ShopifyProduct,
    ShopifyProductCollection,
    ShopifySyncRun,
    ShopifyVariant,
)
from gglads.services import integrations as integrations_svc

logger = logging.getLogger("gglads.shopify")


_COLLECTIONS_QUERY = """
query ($cursor: String) {
  collections(first: 100, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      legacyResourceId
      handle
      title
      descriptionHtml
      image { url }
      productsCount { count }
    }
  }
}
"""


_PRODUCTS_QUERY = """
query ($cursor: String) {
  products(first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      legacyResourceId
      handle
      title
      descriptionHtml
      vendor
      productType
      status
      createdAt
      updatedAt
      totalInventory
      featuredImage { url }
      priceRangeV2 {
        minVariantPrice { amount currencyCode }
        maxVariantPrice { amount currencyCode }
      }
      variants(first: 100) {
        nodes {
          id
          legacyResourceId
          sku
          title
          price
          inventoryQuantity
          selectedOptions { name value }
        }
      }
      collections(first: 100) {
        nodes { id legacyResourceId }
      }
    }
  }
}
"""


def _normalize_domain(domain: str) -> str:
    domain = domain.strip().rstrip("/").replace("https://", "").replace("http://", "")
    if not domain.endswith(".myshopify.com"):
        if "." not in domain:
            domain = f"{domain}.myshopify.com"
    return domain


def _shopify_admin_url(domain: str, product_id: int) -> str:
    return f"https://{domain}/admin/products/{product_id}"


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _post_graphql(
    client: httpx.Client, url: str, headers: dict, query: str, variables: dict
) -> dict[str, Any]:
    r = client.post(url, headers=headers, json={"query": query, "variables": variables})
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise RuntimeError(f"Shopify GraphQL error: {data['errors']}")
    return data["data"]


def _upsert_collection(db: Session, node: dict) -> int:
    cid = int(node["legacyResourceId"])
    existing = db.get(ShopifyCollection, cid)
    if existing is None:
        existing = ShopifyCollection(id=cid)
        db.add(existing)
    existing.handle = node["handle"]
    existing.title = node["title"]
    existing.description = node.get("descriptionHtml")
    image = node.get("image") or {}
    existing.image_url = image.get("url")
    existing.product_count = (node.get("productsCount") or {}).get("count") or 0
    existing.synced_at = datetime.now(timezone.utc)
    return cid


def _upsert_product(
    db: Session, node: dict, domain: str, collection_gid_to_id: dict[str, int]
) -> int:
    pid = int(node["legacyResourceId"])
    existing = db.get(ShopifyProduct, pid)
    if existing is None:
        existing = ShopifyProduct(id=pid)
        db.add(existing)

    price_range = node.get("priceRangeV2") or {}
    min_price = (price_range.get("minVariantPrice") or {})
    max_price = (price_range.get("maxVariantPrice") or {})

    existing.handle = node["handle"]
    existing.title = node["title"]
    existing.description_html = node.get("descriptionHtml")
    existing.vendor = node.get("vendor")
    existing.product_type = node.get("productType")
    existing.status = (node.get("status") or "active").lower()
    featured = node.get("featuredImage") or {}
    existing.image_url = featured.get("url")
    existing.price_min = Decimal(min_price["amount"]) if min_price.get("amount") else None
    existing.price_max = Decimal(max_price["amount"]) if max_price.get("amount") else None
    existing.currency = min_price.get("currencyCode") or max_price.get("currencyCode")
    existing.total_inventory = node.get("totalInventory") or 0
    existing.created_at = _parse_iso(node.get("createdAt"))
    existing.updated_at = _parse_iso(node.get("updatedAt"))
    existing.shopify_admin_url = _shopify_admin_url(domain, pid)
    existing.synced_at = datetime.now(timezone.utc)

    # Variants — replace wholesale (delete then re-insert) so removed variants vanish
    db.execute(delete(ShopifyVariant).where(ShopifyVariant.product_id == pid))
    variant_nodes = (node.get("variants") or {}).get("nodes") or []
    first_sku = None
    for v in variant_nodes:
        opts = v.get("selectedOptions") or []
        opt_values = [o.get("value") for o in opts]
        variant = ShopifyVariant(
            id=int(v["legacyResourceId"]),
            product_id=pid,
            sku=v.get("sku") or None,
            title=v.get("title"),
            price=Decimal(v["price"]) if v.get("price") else None,
            inventory_quantity=v.get("inventoryQuantity") or 0,
            option1=opt_values[0] if len(opt_values) > 0 else None,
            option2=opt_values[1] if len(opt_values) > 1 else None,
            option3=opt_values[2] if len(opt_values) > 2 else None,
        )
        db.add(variant)
        if first_sku is None and v.get("sku"):
            first_sku = v.get("sku")
    existing.variant_count = len(variant_nodes)
    existing.first_sku = first_sku

    # Collection memberships — replace wholesale
    db.execute(
        delete(ShopifyProductCollection).where(ShopifyProductCollection.product_id == pid)
    )
    for c in (node.get("collections") or {}).get("nodes") or []:
        cid_legacy = int(c["legacyResourceId"])
        # Only insert if the collection exists in our DB (we synced collections first)
        if cid_legacy in collection_gid_to_id.values():
            db.add(ShopifyProductCollection(product_id=pid, collection_id=cid_legacy))

    return pid


def sync_catalog(db: Session) -> tuple[bool, str, dict]:
    """Run a full sync. Returns (ok, detail, stats)."""
    cfg = integrations_svc.get_config(db, "shopify")
    if not integrations_svc.is_configured(cfg, integrations_svc.required_keys("shopify")):
        return False, "Shopify is not connected.", {}

    domain = _normalize_domain(cfg["store_domain"])
    token = cfg["admin_api_token"].strip()
    version = (cfg.get("api_version") or "2025-01").strip()
    url = f"https://{domain}/admin/api/{version}/graphql.json"
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}

    run = ShopifySyncRun()
    db.add(run)
    db.commit()
    db.refresh(run)

    try:
        with httpx.Client(timeout=60.0) as client:
            # 1) Collections
            collection_count = 0
            collection_gid_to_id: dict[str, int] = {}
            cursor: str | None = None
            while True:
                data = _post_graphql(
                    client, url, headers, _COLLECTIONS_QUERY, {"cursor": cursor}
                )
                page = data["collections"]
                for node in page["nodes"]:
                    cid = _upsert_collection(db, node)
                    collection_gid_to_id[node["id"]] = cid
                    collection_count += 1
                db.commit()
                if not page["pageInfo"]["hasNextPage"]:
                    break
                cursor = page["pageInfo"]["endCursor"]

            # 2) Products
            product_count = 0
            cursor = None
            while True:
                data = _post_graphql(
                    client, url, headers, _PRODUCTS_QUERY, {"cursor": cursor}
                )
                page = data["products"]
                for node in page["nodes"]:
                    _upsert_product(db, node, domain, collection_gid_to_id)
                    product_count += 1
                db.commit()
                if not page["pageInfo"]["hasNextPage"]:
                    break
                cursor = page["pageInfo"]["endCursor"]

        run.finished_at = datetime.now(timezone.utc)
        run.ok = True
        run.products_count = product_count
        run.collections_count = collection_count
        run.detail = f"Synced {product_count} products and {collection_count} collections."
        db.commit()
        return True, run.detail, {
            "products": product_count,
            "collections": collection_count,
        }
    except httpx.HTTPError as exc:
        msg = f"Network error: {type(exc).__name__}: {exc}"
        run.finished_at = datetime.now(timezone.utc)
        run.ok = False
        run.detail = msg
        db.commit()
        logger.exception("Shopify sync failed")
        return False, msg, {}
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        run.finished_at = datetime.now(timezone.utc)
        run.ok = False
        run.detail = msg
        db.commit()
        logger.exception("Shopify sync failed")
        return False, msg, {}


def last_sync_run(db: Session) -> ShopifySyncRun | None:
    return db.scalar(
        select(ShopifySyncRun).order_by(ShopifySyncRun.started_at.desc()).limit(1)
    )
