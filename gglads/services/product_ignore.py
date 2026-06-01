"""Global product Ignore — hides a product from the default views and skips
it in bulk operations. Sync still keeps the row fresh from Shopify so a
later un-ignore lands on accurate data.

Operations that respect the flag:
  - default /products list (set include_ignored=True to override)
  - keyword_research.research_all_products (skips ignored)

Operations that DO NOT respect it (intentional):
  - Catalog/sales/inventory syncs from Shopify — keep data fresh.
  - Per-product pages (you can still open an ignored product directly).
"""

from __future__ import annotations

import logging

from sqlalchemy import update
from sqlalchemy.orm import Session

from gglads.models.shopify_product import (
    ShopifyCollection,
    ShopifyProduct,
    ShopifyProductCollection,
)

logger = logging.getLogger("gglads.product_ignore")


def ignore_products(db: Session, product_ids: list[int]) -> tuple[bool, str, int]:
    if not product_ids:
        return False, "No products selected.", 0
    n = db.execute(
        update(ShopifyProduct)
        .where(ShopifyProduct.id.in_(product_ids))
        .where(ShopifyProduct.is_ignored.is_(False))
        .values(is_ignored=True)
    ).rowcount or 0
    db.commit()
    if n == 0:
        return False, "All selected products were already ignored.", 0
    return True, f"Ignored {n} product(s).", int(n)


def unignore_products(db: Session, product_ids: list[int]) -> tuple[bool, str, int]:
    if not product_ids:
        return False, "No products selected.", 0
    n = db.execute(
        update(ShopifyProduct)
        .where(ShopifyProduct.id.in_(product_ids))
        .where(ShopifyProduct.is_ignored.is_(True))
        .values(is_ignored=False)
    ).rowcount or 0
    db.commit()
    if n == 0:
        return False, "None of the selected products were ignored.", 0
    return True, f"Un-ignored {n} product(s). They're back in the default views.", int(n)


def ignore_all_matching(
    db: Session,
    *,
    q: str | None = None,
    status_filter: str | None = None,
    collection_handle: str | None = None,
    include_drafts: bool = True,
) -> tuple[bool, str, int]:
    """Ignore every product matching the given filter, in a single UPDATE.
    Mirrors the same filter logic as the products list so 'select all matching'
    behaves predictably."""
    from sqlalchemy import select

    stmt = select(ShopifyProduct.id).where(ShopifyProduct.is_ignored.is_(False))
    if q:
        stmt = stmt.where(ShopifyProduct.title.ilike(f"%{q.strip()}%"))
    if status_filter:
        stmt = stmt.where(ShopifyProduct.status == status_filter)
    elif not include_drafts:
        stmt = stmt.where(ShopifyProduct.status != "draft")
    if collection_handle:
        stmt = (
            stmt.join(
                ShopifyProductCollection,
                ShopifyProductCollection.product_id == ShopifyProduct.id,
            )
            .join(
                ShopifyCollection,
                ShopifyCollection.id == ShopifyProductCollection.collection_id,
            )
            .where(ShopifyCollection.handle == collection_handle)
        )
    ids = list(db.execute(stmt).scalars().all())
    if not ids:
        return False, "Nothing to ignore — current filter matched no un-ignored products.", 0
    return ignore_products(db, ids)
