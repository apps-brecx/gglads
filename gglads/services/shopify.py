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
    ShopifyDailySales,
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

# We only attribute sales from these two Shopify channels:
#   web  → Online Store
#   shop → Shop app
# Anything else (POS, draft orders, third-party app channels) is dropped at
# ingest. Use a set so membership checks are O(1).
TRACKED_CHANNELS = {"web", "shop"}


def _normalize_channel(source_name: str | None) -> str | None:
    """Map a Shopify Order.sourceName to one of TRACKED_CHANNELS, or None to drop."""
    if not source_name:
        return None
    s = source_name.lower().strip()
    if s in TRACKED_CHANNELS:
        return s
    # Shopify has historically used a few variants for the Shop app.
    if s in {"shop_app", "shopify_app"}:
        return "shop"
    return None


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
      sourceName
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
    """Pull orders in the time window, aggregate sales per product, write.

    Two outputs:
      1. Per-product 90-day totals on shopify_products (back-compat for the
         Keywords / product pages).
      2. Per-day per-product per-channel rollup in shopify_daily_sales — this
         is what the new dashboard reads from.

    Only orders whose sourceName maps to TRACKED_CHANNELS are counted; POS,
    draft orders, etc. are silently dropped at ingest.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    cutoff_date = cutoff.date()
    q = f"created_at:>={cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')}"

    # Reset all product counters first (existing 90d aggregates).
    db.execute(
        update(ShopifyProduct).values(
            units_sold_90d=0,
            unique_customers_90d=0,
            last_sale_at=None,
            net_sales_90d=Decimal("0"),
        )
    )
    # Clear the window from shopify_daily_sales so a re-sync produces a
    # consistent state (no stale rows for the same date+product+channel).
    db.execute(
        delete(ShopifyDailySales).where(
            ShopifyDailySales.snapshot_date >= cutoff_date
        )
    )
    db.commit()

    # Per-product 90d aggregates (back-compat).
    agg: dict[int, dict] = {}
    # Per-day rollups, keyed by (date, product_id|None, channel).
    daily: dict[tuple, dict] = {}
    # Per-day per-channel store-wide totals are not built from the per-product
    # rows (a single order with N line items is 1 order, not N). We track
    # them separately, keyed by (date, channel), so the "all products"
    # rollup row reflects orders / unique customers correctly.
    store_daily: dict[tuple, dict] = {}

    cursor: str | None = None
    orders_seen = 0
    orders_kept = 0
    # Per-channel breakdown of every source_name we encounter, kept vs dropped.
    # Logged at end of sync so the user can audit exactly what was filtered.
    channels_kept_count: dict[str, int] = {}
    channels_dropped_count: dict[str, int] = {}

    while True:
        data = _post_graphql(
            client, url, headers, _ORDERS_QUERY, {"cursor": cursor, "q": q}
        )
        page = data["orders"]
        for order in page["nodes"]:
            if order.get("cancelledAt"):
                continue
            orders_seen += 1
            source_name = (order.get("sourceName") or "(unknown)").lower().strip()
            channel = _normalize_channel(order.get("sourceName"))
            if channel is None:
                channels_dropped_count[source_name] = (
                    channels_dropped_count.get(source_name, 0) + 1
                )
                continue
            channels_kept_count[channel] = channels_kept_count.get(channel, 0) + 1
            order_date_dt = _parse_iso(order.get("createdAt"))
            if order_date_dt is None:
                continue
            order_date = order_date_dt.date()
            customer_id = (order.get("customer") or {}).get("id")
            orders_kept += 1

            # Store-wide (product_id NULL) rollup for this (date, channel).
            store_key = (order_date, channel)
            store_entry = store_daily.setdefault(
                store_key,
                {
                    "orders": 0,
                    "units": 0,
                    "revenue": Decimal("0"),
                    "customers": set(),
                },
            )
            store_entry["orders"] += 1
            if customer_id:
                store_entry["customers"].add(customer_id)

            # Track which products this order touched so we count each
            # product as 1 "order" per (date, channel) — not N (one per line
            # item of that product), but also not 0.
            products_in_order: set[int] = set()
            for li in (order.get("lineItems") or {}).get("nodes") or []:
                product_gid = (li.get("product") or {}).get("legacyResourceId")
                if not product_gid:
                    continue
                try:
                    pid = int(product_gid)
                except ValueError:
                    continue
                qty = li.get("quantity") or 0
                discounted = (
                    (li.get("discountedTotalSet") or {}).get("shopMoney") or {}
                )
                line_amount = Decimal(discounted.get("amount") or "0")

                # Back-compat per-product 90d aggregates.
                back = agg.setdefault(
                    pid,
                    {
                        "units": 0,
                        "customers": set(),
                        "last_sale": None,
                        "revenue": Decimal("0"),
                    },
                )
                back["units"] += qty
                back["revenue"] += line_amount
                if customer_id:
                    back["customers"].add(customer_id)
                if back["last_sale"] is None or order_date_dt > back["last_sale"]:
                    back["last_sale"] = order_date_dt

                # Per-day per-product per-channel rollup.
                store_entry["units"] += qty
                store_entry["revenue"] += line_amount
                key = (order_date, pid, channel)
                ent = daily.setdefault(
                    key,
                    {
                        "orders": 0,
                        "units": 0,
                        "revenue": Decimal("0"),
                        "customers": set(),
                    },
                )
                ent["units"] += qty
                ent["revenue"] += line_amount
                if customer_id:
                    ent["customers"].add(customer_id)
                if pid not in products_in_order:
                    ent["orders"] += 1
                    products_in_order.add(pid)

        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]

    # Write per-product 90d aggregates.
    for pid, data in agg.items():
        p = db.get(ShopifyProduct, pid)
        if p is not None:
            p.units_sold_90d = data["units"]
            p.unique_customers_90d = len(data["customers"])
            p.last_sale_at = data["last_sale"]
            p.net_sales_90d = data["revenue"]

    # Write per-product per-day per-channel rollups.
    now = datetime.now(timezone.utc)
    for (day, pid, channel), v in daily.items():
        db.add(
            ShopifyDailySales(
                snapshot_date=day,
                product_id=pid,
                channel=channel,
                orders=v["orders"],
                units=v["units"],
                revenue=v["revenue"],
                unique_customers=len(v["customers"]),
                updated_at=now,
            )
        )

    # Write store-wide (product_id NULL) rollups.
    for (day, channel), v in store_daily.items():
        db.add(
            ShopifyDailySales(
                snapshot_date=day,
                product_id=None,
                channel=channel,
                orders=v["orders"],
                units=v["units"],
                revenue=v["revenue"],
                unique_customers=len(v["customers"]),
                updated_at=now,
            )
        )
    db.commit()
    kept_summary = ", ".join(
        f"{k}={v}" for k, v in sorted(channels_kept_count.items())
    ) or "(none)"
    dropped_summary = ", ".join(
        f"{k}={v}" for k, v in sorted(channels_dropped_count.items())
    ) or "(none)"
    logger.info(
        "Order sync: %d seen, %d kept. KEPT: %s | DROPPED (non-tracked channels): %s",
        orders_seen, orders_kept, kept_summary, dropped_summary,
    )
    return {
        "orders_seen": orders_seen,
        "orders_kept": orders_kept,
        "channels_kept": channels_kept_count,
        "channels_dropped": channels_dropped_count,
    }


# ---------------------------------------------------------------------------
# Sync entry points
# ---------------------------------------------------------------------------

def sync_full(db: Session) -> tuple[bool, str, dict]:
    """Catalog + sales + inventory snapshot."""
    return _run(db, kind="full", catalog=True, orders=True, snapshot=True)


def sync_catalog_only(db: Session) -> tuple[bool, str, dict]:
    """Collections + publications + products + images. No orders, no snapshot."""
    return _run(db, kind="catalog", catalog=True, orders=False, snapshot=False)


def sync_sales_only(db: Session) -> tuple[bool, str, dict]:
    """Orders (units/customers/revenue) + today's inventory snapshot. Fast."""
    return _run(db, kind="sales", catalog=False, orders=True, snapshot=True)


def sync_inventory_only(db: Session) -> tuple[bool, str, dict]:
    """Just write today's inventory snapshot from already-synced product data."""
    return _run(db, kind="inventory", catalog=False, orders=False, snapshot=True)


# Backwards-compat alias (cron + any legacy callers).
sync_catalog = sync_full


def _run(
    db: Session,
    *,
    kind: str,
    catalog: bool,
    orders: bool,
    snapshot: bool,
) -> tuple[bool, str, dict]:
    cfg = integrations_svc.get_config(db, "shopify")
    if not integrations_svc.is_configured(cfg, integrations_svc.required_keys("shopify")):
        return False, "Shopify is not connected.", {}

    domain = _normalize_domain(cfg["store_domain"])
    token = cfg["admin_api_token"].strip()
    version = (cfg.get("api_version") or "2025-01").strip()
    url = f"https://{domain}/admin/api/{version}/graphql.json"
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}

    run = ShopifySyncRun(kind=kind)
    db.add(run)
    db.commit()
    db.refresh(run)

    collection_count = 0
    product_count = 0
    orders_count = 0
    orders_kept = 0
    channels_kept: dict[str, int] = {}
    channels_dropped: dict[str, int] = {}
    snapshots_count = 0

    try:
        with httpx.Client(timeout=120.0) as client:
            collection_legacy_ids: set[int] = set()
            publication_legacy_ids: set[int] = set()

            if catalog:
                collection_count, collection_legacy_ids = _phase_collections(
                    db, client, url, headers
                )
                publication_legacy_ids = _phase_publications(db, client, url, headers)
                product_count = _phase_products(
                    db,
                    client,
                    url,
                    headers,
                    domain,
                    collection_legacy_ids,
                    publication_legacy_ids,
                )

            if snapshot:
                snapshots_count = _record_inventory_snapshots(db)

            if orders:
                try:
                    order_stats = _sync_orders(client, url, headers, db)
                    orders_count = order_stats["orders_seen"]
                    orders_kept = order_stats["orders_kept"]
                    channels_kept = order_stats["channels_kept"]
                    channels_dropped = order_stats["channels_dropped"]
                except RuntimeError as exc:
                    logger.warning("Orders sync skipped: %s", exc)

        # Bring oos_since / oos_ignored in line with the freshly-synced
        # total_inventory values. Cheap (3 UPDATEs) so we always run it.
        from gglads.services import oos as oos_svc
        oos_svc.reconcile_oos_state(db)

        bits: list[str] = []
        if catalog:
            bits.append(f"{product_count} products")
            bits.append(f"{collection_count} collections")
        if orders:
            bits.append(
                f"{orders_kept} of {orders_count} orders kept "
                f"(last {SALES_WINDOW_DAYS} days, channels: "
                f"{', '.join(sorted(TRACKED_CHANNELS))})"
            )
            if channels_dropped:
                bits.append(
                    "Dropped channels: "
                    + ", ".join(f"{k}={v}" for k, v in sorted(channels_dropped.items()))
                )
        if snapshot:
            bits.append(f"{snapshots_count} stock snapshot(s)")
        detail = f"[{kind}] " + ", ".join(bits) + "."

        run.finished_at = datetime.now(timezone.utc)
        run.ok = True
        run.products_count = product_count
        run.collections_count = collection_count
        run.orders_count = orders_count
        run.detail = detail
        db.commit()
        return True, detail, {
            "kind": kind,
            "products": product_count,
            "collections": collection_count,
            "orders_seen": orders_count,
            "orders_kept": orders_kept,
            "channels_kept": channels_kept,
            "channels_dropped": channels_dropped,
            "snapshots": snapshots_count,
        }

    except httpx.HTTPError as exc:
        msg = f"[{kind}] Network error: {type(exc).__name__}: {exc}"
        logger.exception("Shopify sync failed")
        return _finish_run_with_error(db, run, msg), msg, {}
    except Exception as exc:  # noqa: BLE001 — catch-all so the request doesn't 500
        msg = f"[{kind}] {type(exc).__name__}: {exc}"
        logger.exception("Shopify sync failed")
        return _finish_run_with_error(db, run, msg), msg, {}


# ---------------------------------------------------------------------------
# Per-phase helpers
# ---------------------------------------------------------------------------

def _phase_collections(db, client, url, headers) -> tuple[int, set[int]]:
    count = 0
    ids: set[int] = set()
    cursor: str | None = None
    while True:
        data = _post_graphql(client, url, headers, _COLLECTIONS_QUERY, {"cursor": cursor})
        page = data["collections"]
        for node in page["nodes"]:
            ids.add(_upsert_collection(db, node))
            count += 1
        db.commit()
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    return count, ids


def _phase_publications(db, client, url, headers) -> set[int]:
    ids: set[int] = set()
    cursor: str | None = None
    try:
        while True:
            data = _post_graphql(
                client, url, headers, _PUBLICATIONS_QUERY, {"cursor": cursor}
            )
            page = data["publications"]
            for node in page["nodes"]:
                legacy = _upsert_publication(db, node)
                if legacy is not None:
                    ids.add(legacy)
            db.commit()
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]
    except RuntimeError as exc:
        # `read_publications` scope might be missing — log and continue
        logger.warning("Publications fetch skipped: %s", exc)
    return ids


def _phase_products(
    db,
    client,
    url,
    headers,
    domain,
    collection_ids: set[int],
    publication_ids: set[int],
) -> int:
    count = 0
    cursor: str | None = None
    while True:
        data = _post_graphql(client, url, headers, _PRODUCTS_QUERY, {"cursor": cursor})
        page = data["products"]
        for node in page["nodes"]:
            _upsert_product(db, node, domain, collection_ids, publication_ids)
            count += 1
        db.commit()
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    return count


def _finish_run_with_error(db: Session, run: ShopifySyncRun, msg: str) -> bool:
    """Record the failure on the run row even if the session was rolled back."""
    db.rollback()  # safe if there's nothing to roll back
    fresh_run = db.get(ShopifySyncRun, run.id)
    if fresh_run is not None:
        fresh_run.finished_at = datetime.now(timezone.utc)
        fresh_run.ok = False
        fresh_run.detail = msg[:1000]
        try:
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
    return False


def last_sync_run(db: Session) -> ShopifySyncRun | None:
    return db.scalar(
        select(ShopifySyncRun).order_by(ShopifySyncRun.started_at.desc()).limit(1)
    )


def last_sync_runs_by_kind(db: Session) -> dict[str, ShopifySyncRun]:
    """Return {kind: latest run} so the UI can show one timestamp per kind."""
    out: dict[str, ShopifySyncRun] = {}
    for kind in ("full", "catalog", "sales", "inventory"):
        run = db.scalar(
            select(ShopifySyncRun)
            .where(ShopifySyncRun.kind == kind)
            .order_by(ShopifySyncRun.started_at.desc())
            .limit(1)
        )
        if run is not None:
            out[kind] = run
    return out
