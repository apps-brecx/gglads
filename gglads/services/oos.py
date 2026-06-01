"""Out-of-stock tracking + per-product Ignore flag.

State model (lives on shopify_products):
  oos_since   — set when total_inventory transitions >0 → 0; cleared on restock.
  oos_ignored — user flag. Set when the user clicks Ignore on an OOS product.
                Automatically cleared when the product comes back in stock, so
                the next OOS spell makes the product visible again.

`reconcile_oos_state` is the single source of truth that updates both columns
based on the current total_inventory. It's called from every Shopify sync
function (catalog / inventory / sales) so the state is always consistent
after a sync.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import and_, func, select, update
from sqlalchemy.orm import Session

from gglads.models.shopify_product import (
    ShopifyCollection,
    ShopifyProduct,
    ShopifyProductCollection,
)

logger = logging.getLogger("gglads.oos")


def reconcile_oos_state(db: Session) -> dict:
    """Sweep all products and bring oos_since / oos_ignored in line with the
    current total_inventory. Returns counters for logging.

    Rules:
      total_inventory == 0 and oos_since IS NULL → set oos_since = now()
      total_inventory  > 0 and oos_since IS NOT NULL → clear oos_since
      total_inventory  > 0 and oos_ignored is true → clear oos_ignored
    """
    now = datetime.now(timezone.utc)

    went_oos = db.execute(
        update(ShopifyProduct)
        .where(ShopifyProduct.total_inventory == 0)
        .where(ShopifyProduct.oos_since.is_(None))
        .values(oos_since=now)
    ).rowcount

    came_back = db.execute(
        update(ShopifyProduct)
        .where(ShopifyProduct.total_inventory > 0)
        .where(ShopifyProduct.oos_since.is_not(None))
        .values(oos_since=None)
    ).rowcount

    cleared_ignores = db.execute(
        update(ShopifyProduct)
        .where(ShopifyProduct.total_inventory > 0)
        .where(ShopifyProduct.oos_ignored.is_(True))
        .values(oos_ignored=False)
    ).rowcount

    db.commit()

    stats = {
        "went_oos": int(went_oos or 0),
        "came_back_in_stock": int(came_back or 0),
        "cleared_ignores": int(cleared_ignores or 0),
    }
    if any(stats.values()):
        logger.info("OOS reconcile: %s", stats)
    return stats


def list_out_of_stock(
    db: Session,
    include_ignored: bool = False,
    collection_handle: str | None = None,
) -> list[dict]:
    """Products that are currently out of stock. By default, ignored ones
    are hidden — pass include_ignored=True for the 'show all OOS' toggle."""
    stmt = select(ShopifyProduct).where(ShopifyProduct.total_inventory == 0)
    if not include_ignored:
        stmt = stmt.where(ShopifyProduct.oos_ignored.is_(False))
    if collection_handle:
        stmt = stmt.join(
            ShopifyProductCollection,
            ShopifyProductCollection.product_id == ShopifyProduct.id,
        ).join(
            ShopifyCollection,
            and_(
                ShopifyProductCollection.collection_id == ShopifyCollection.id,
                ShopifyCollection.handle == collection_handle,
            ),
        )
    stmt = stmt.order_by(
        ShopifyProduct.oos_since.asc().nullsfirst(),  # oldest OOS first
        ShopifyProduct.title,
    )
    products = db.execute(stmt).scalars().unique().all()
    now = datetime.now(timezone.utc)
    out: list[dict] = []
    for p in products:
        days_oos: int | None = None
        if p.oos_since:
            days_oos = max(0, (now - p.oos_since).days)
        out.append({
            "id": p.id,
            "title": p.title,
            "handle": p.handle,
            "image_url": p.image_url,
            "status": p.status,
            "oos_since": p.oos_since,
            "oos_ignored": p.oos_ignored,
            "days_oos": days_oos,
            "units_sold_90d": p.units_sold_90d,
            "net_sales_90d": p.net_sales_90d,
            "last_sale_at": p.last_sale_at,
            "variant_count": p.variant_count,
        })
    return out


def oos_counts(db: Session) -> dict:
    """Quick KPI counters for the page header."""
    total_oos = db.scalar(
        select(func.count(ShopifyProduct.id)).where(
            ShopifyProduct.total_inventory == 0
        )
    ) or 0
    ignored = db.scalar(
        select(func.count(ShopifyProduct.id)).where(
            ShopifyProduct.total_inventory == 0,
            ShopifyProduct.oos_ignored.is_(True),
        )
    ) or 0
    visible = total_oos - ignored
    return {"total_oos": int(total_oos), "ignored": int(ignored), "visible": int(visible)}


def ignore_product(db: Session, product_id: int) -> tuple[bool, str]:
    """Mark this product as ignored. Allowed regardless of current stock; if
    the product is in stock, the flag will be cleared on the next sync anyway."""
    p = db.get(ShopifyProduct, product_id)
    if p is None:
        return False, "Product not found."
    if p.total_inventory > 0:
        return False, "Product is in stock — nothing to ignore."
    if p.oos_ignored:
        return False, "Already ignored."
    p.oos_ignored = True
    db.commit()
    return True, f'"{p.title}" ignored. It will reappear if it goes OOS again after being restocked.'


def unignore_product(db: Session, product_id: int) -> tuple[bool, str]:
    p = db.get(ShopifyProduct, product_id)
    if p is None:
        return False, "Product not found."
    if not p.oos_ignored:
        return False, "Not ignored."
    p.oos_ignored = False
    db.commit()
    return True, f'"{p.title}" is no longer ignored.'


def bulk_ignore(db: Session, product_ids: list[int]) -> tuple[bool, str, int]:
    if not product_ids:
        return False, "No products selected.", 0
    n = db.execute(
        update(ShopifyProduct)
        .where(ShopifyProduct.id.in_(product_ids))
        .where(ShopifyProduct.total_inventory == 0)
        .where(ShopifyProduct.oos_ignored.is_(False))
        .values(oos_ignored=True)
    ).rowcount or 0
    db.commit()
    if n == 0:
        return False, "Nothing to ignore (selected products are either in stock or already ignored).", 0
    return True, f"Ignored {n} product(s). They'll reappear after their next restock+OOS cycle.", int(n)


def bulk_unignore(db: Session, product_ids: list[int]) -> tuple[bool, str, int]:
    if not product_ids:
        return False, "No products selected.", 0
    n = db.execute(
        update(ShopifyProduct)
        .where(ShopifyProduct.id.in_(product_ids))
        .where(ShopifyProduct.oos_ignored.is_(True))
        .values(oos_ignored=False)
    ).rowcount or 0
    db.commit()
    if n == 0:
        return False, "Nothing was ignored among selected.", 0
    return True, f"Un-ignored {n} product(s).", int(n)
