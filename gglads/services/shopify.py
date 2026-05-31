"""Shopify Admin API sync.

Pulls the full catalog (collections + products + variants + memberships) into
the DB via the GraphQL Admin API. Pagination handled with cursors.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from gglads.models.shopify_product import (
    ShopifyCollection,
    ShopifyInventorySnapshot,
    ShopifyProduct,
    ShopifyProductCollection,
    ShopifyProductImage,
    ShopifyProductPublication,
    ShopifyPublication,
    ShopifySyncRun,
    ShopifyVariant,
)
from gglads.services import integrations as integrations_svc

logger = logging.getLogger("gglads.shopify")

SALES_WINDOW_DAYS = 90


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
      seo { title description }
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
      resourcePublications(first: 50) {
        nodes {
          isPublished
          publication { id name }
        }
      }
      images(first: 50) {
        nodes {
          id
          url
          altText
          width
          height
        }
      }
    }
  }
}
"""


_PUBLICATIONS_QUERY = """
query ($cursor: String) {
  publications(first: 100, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes { id name }
  }
}
"""


_ORDERS_QUERY = """
query ($cursor: String, $q: String) {
  orders(first: 50, after: $cursor, query: $q) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      createdAt
      cancelledAt
      customer { id }
      lineItems(first: 100) {
        nodes {
          quantity
          discountedTotalSet { shopMoney { amount } }
          product { legacyResourceId }
        }
      }
    }
  }
}
"""


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "unknown"


def _legacy_id_from_gid(gid: str | None) -> int | None:
    if not gid:
        return None
    try:
        return int(gid.rsplit("/", 1)[-1])
    except (ValueError, AttributeError):
        return None


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


def _upsert_publication(db: Session, node: dict) -> int | None:
    legacy = _legacy_id_from_gid(node.get("id"))
    if legacy is None:
        return None
    existing = db.get(ShopifyPublication, legacy)
    if existing is None:
        existing = ShopifyPublication(id=legacy)
        db.add(existing)
    existing.name = node.get("name") or "Unknown"
    existing.slug = _slugify(existing.name)
    existing.synced_at = datetime.now(timezone.utc)
    return legacy


def _upsert_product(
    db: Session,
    node: dict,
    domain: str,
    collection_legacy_ids: set[int],
    publication_legacy_ids: set[int],
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
    seo = node.get("seo") or {}
    existing.seo_title = seo.get("title") or None
    existing.seo_meta_description = seo.get("description") or None
    existing.price_min = Decimal(min_price["amount"]) if min_price.get("amount") else None
    existing.price_max = Decimal(max_price["amount"]) if max_price.get("amount") else None
    existing.currency = min_price.get("currencyCode") or max_price.get("currencyCode")
    existing.total_inventory = node.get("totalInventory") or 0
    existing.created_at = _parse_iso(node.get("createdAt"))
    existing.updated_at = _parse_iso(node.get("updatedAt"))
    existing.shopify_admin_url = _shopify_admin_url(domain, pid)
    existing.synced_at = datetime.now(timezone.utc)

    # Product images — replace wholesale
    db.execute(
        delete(ShopifyProductImage).where(ShopifyProductImage.product_id == pid)
    )
    for position, img in enumerate((node.get("images") or {}).get("nodes") or []):
        img_legacy = _legacy_id_from_gid(img.get("id"))
        if img_legacy is None or not img.get("url"):
            continue
        db.add(
            ShopifyProductImage(
                id=img_legacy,
                product_id=pid,
                position=position,
                url=img["url"],
                alt_text=img.get("altText"),
                width=img.get("width"),
                height=img.get("height"),
            )
        )

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
        if cid_legacy in collection_legacy_ids:
            db.add(ShopifyProductCollection(product_id=pid, collection_id=cid_legacy))

    # Publication memberships — only "isPublished" ones, replace wholesale
    db.execute(
        delete(ShopifyProductPublication).where(ShopifyProductPublication.product_id == pid)
    )
    for rp in (node.get("resourcePublications") or {}).get("nodes") or []:
        if not rp.get("isPublished"):
            continue
        pub = rp.get("publication") or {}
        pub_legacy = _legacy_id_from_gid(pub.get("id"))
        if pub_legacy is not None and pub_legacy in publication_legacy_ids:
            db.add(
                ShopifyProductPublication(product_id=pid, publication_id=pub_legacy)
            )

    return pid


def _record_inventory_snapshots(db: Session) -> int:
    """Write today's inventory snapshot for every product. Upserts by date."""
    today = datetime.now(timezone.utc).date()
    products = db.execute(
        select(ShopifyProduct.id, ShopifyProduct.total_inventory)
    ).all()
    if not products:
        return 0
    # Remove any prior snapshot for today (cheap idempotency)
    db.execute(
        delete(ShopifyInventorySnapshot).where(
            ShopifyInventorySnapshot.snapshot_date == today
        )
    )
    for pid, inv in products:
        inv_int = int(inv or 0)
        db.add(
            ShopifyInventorySnapshot(
                product_id=pid,
                snapshot_date=today,
                inventory=inv_int,
                is_in_stock=(inv_int > 0),
            )
        )
    db.commit()
    return len(products)


def _sync_orders(
    client: httpx.Client,
    url: str,
    headers: dict,
    db: Session,
    window_days: int = SALES_WINDOW_DAYS,
) -> int:
    """Pull orders in the time window, aggregate sales per product, write."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    q = f"created_at:>={cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')}"

    # Reset all product counters first
    db.execute(
        update(ShopifyProduct).values(
            units_sold_90d=0,
            unique_customers_90d=0,
            last_sale_at=None,
            net_sales_90d=Decimal("0"),
        )
    )
    db.commit()

    agg: dict[int, dict] = {}
    cursor: str | None = None
    orders_seen = 0

    while True:
        data = _post_graphql(
            client, url, headers, _ORDERS_QUERY, {"cursor": cursor, "q": q}
        )
        page = data["orders"]
        for order in page["nodes"]:
            if order.get("cancelledAt"):
                continue
            orders_seen += 1
            order_date = _parse_iso(order.get("createdAt"))
            customer_id = (order.get("customer") or {}).get("id")
            for li in (order.get("lineItems") or {}).get("nodes") or []:
                product_gid = (li.get("product") or {}).get("legacyResourceId")
                if not product_gid:
                    continue
                try:
                    pid = int(product_gid)
                except ValueError:
                    continue
                qty = li.get("quantity") or 0
                discounted = (li.get("discountedTotalSet") or {}).get("shopMoney") or {}
                line_amount = Decimal(discounted.get("amount") or "0")
                entry = agg.setdefault(
                    pid,
                    {"units": 0, "customers": set(), "last_sale": None, "revenue": Decimal("0")},
                )
                entry["units"] += qty
                entry["revenue"] += line_amount
                if customer_id:
                    entry["customers"].add(customer_id)
                if order_date and (
                    entry["last_sale"] is None or order_date > entry["last_sale"]
                ):
                    entry["last_sale"] = order_date
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]

    # Write aggregates
    for pid, data in agg.items():
        p = db.get(ShopifyProduct, pid)
        if p is not None:
            p.units_sold_90d = data["units"]
            p.unique_customers_90d = len(data["customers"])
            p.last_sale_at = data["last_sale"]
            p.net_sales_90d = data["revenue"]
    db.commit()

    return orders_seen


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
        with httpx.Client(timeout=120.0) as client:
            # 1) Collections
            collection_count = 0
            collection_legacy_ids: set[int] = set()
            cursor: str | None = None
            while True:
                data = _post_graphql(
                    client, url, headers, _COLLECTIONS_QUERY, {"cursor": cursor}
                )
                page = data["collections"]
                for node in page["nodes"]:
                    cid = _upsert_collection(db, node)
                    collection_legacy_ids.add(cid)
                    collection_count += 1
                db.commit()
                if not page["pageInfo"]["hasNextPage"]:
                    break
                cursor = page["pageInfo"]["endCursor"]

            # 2) Publications (sales channels)
            publication_legacy_ids: set[int] = set()
            cursor = None
            try:
                while True:
                    data = _post_graphql(
                        client, url, headers, _PUBLICATIONS_QUERY, {"cursor": cursor}
                    )
                    page = data["publications"]
                    for node in page["nodes"]:
                        legacy = _upsert_publication(db, node)
                        if legacy is not None:
                            publication_legacy_ids.add(legacy)
                    db.commit()
                    if not page["pageInfo"]["hasNextPage"]:
                        break
                    cursor = page["pageInfo"]["endCursor"]
            except RuntimeError as exc:
                # `read_publications` scope might be missing — log and continue
                logger.warning("Publications fetch skipped: %s", exc)

            # 3) Products
            product_count = 0
            cursor = None
            while True:
                data = _post_graphql(
                    client, url, headers, _PRODUCTS_QUERY, {"cursor": cursor}
                )
                page = data["products"]
                for node in page["nodes"]:
                    _upsert_product(
                        db,
                        node,
                        domain,
                        collection_legacy_ids,
                        publication_legacy_ids,
                    )
                    product_count += 1
                db.commit()
                if not page["pageInfo"]["hasNextPage"]:
                    break
                cursor = page["pageInfo"]["endCursor"]

            # 4) Daily inventory snapshot (used for 30-day stock history)
            _record_inventory_snapshots(db)

            # 5) Orders (sales aggregates, last 90 days) — best-effort
            orders_count = 0
            try:
                orders_count = _sync_orders(client, url, headers, db)
            except RuntimeError as exc:
                logger.warning("Orders sync skipped: %s", exc)

        run.finished_at = datetime.now(timezone.utc)
        run.ok = True
        run.products_count = product_count
        run.collections_count = collection_count
        run.orders_count = orders_count
        run.detail = (
            f"Synced {product_count} products, {collection_count} collections, "
            f"and {orders_count} orders (last {SALES_WINDOW_DAYS} days). "
            f"Inventory snapshot recorded for today."
        )
        db.commit()
        return True, run.detail, {
            "products": product_count,
            "collections": collection_count,
            "orders": orders_count,
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
