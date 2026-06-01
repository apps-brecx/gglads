"""Daily per-product keyword position snapshots + growth-alert detection.

Cron writes one row per (date, product, keyword) per day using Search Console
data. The growth_alerts() helper compares today vs N days ago to surface:
  - new keywords  (first time appearing in SC for this product URL)
  - improved      (position got better by ≥ MOVEMENT_THRESHOLD)
  - regressed     (position got worse by ≥ MOVEMENT_THRESHOLD)
  - top_10        (newly in top 10)
  - top_3         (newly in top 3)
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Sequence

from sqlalchemy import and_, delete, func, select
from sqlalchemy.orm import Session

from gglads.models.product_keywords import ProductKeywordHistory
from gglads.models.shopify_product import ShopifyProduct
from gglads.services import search_console as sc_svc

logger = logging.getLogger("gglads.kw_history")

# A keyword has to move at least this many positions before we flag it as
# an alert. Tuned so noise (rank jitter inside the same SERP page) is filtered.
MOVEMENT_THRESHOLD = 3.0


def snapshot_product(
    db: Session, product_id: int, today: date | None = None
) -> tuple[bool, str, int]:
    """Pull current SC data for one product's URL, upsert today's rows."""
    p = db.get(ShopifyProduct, product_id)
    if p is None:
        return False, "Product not found.", 0
    from gglads.services import integrations as integrations_svc
    cfg = integrations_svc.get_config(db, "google_search_console")
    site_url = (cfg.get("site_url") or "").strip()
    if not site_url:
        return False, "Search Console not configured.", 0
    page_url = sc_svc.page_url_from_site(site_url, p.handle)
    if not page_url:
        return False, "Could not compose page URL.", 0

    rows, err = sc_svc.get_queries_for_page(db, page_url, days=1, row_limit=100)
    if err:
        return False, err, 0
    if not rows:
        return True, "No SC data for this URL today.", 0

    day = today or date.today()
    # Clear today's prior rows for idempotency.
    db.execute(
        delete(ProductKeywordHistory)
        .where(ProductKeywordHistory.snapshot_date == day)
        .where(ProductKeywordHistory.product_id == product_id)
    )
    written = 0
    for r in rows:
        kw = (r.get("query") or "").strip().lower()[:255]
        if not kw:
            continue
        db.add(
            ProductKeywordHistory(
                snapshot_date=day,
                product_id=product_id,
                keyword=kw,
                sc_position=r.get("position"),
                sc_clicks=r.get("clicks"),
                sc_impressions=r.get("impressions"),
                sc_ctr=r.get("ctr"),
            )
        )
        written += 1
    db.commit()
    return True, f"Snapshotted {written} keyword(s) for product {product_id}.", written


def snapshot_all_products(
    db: Session, today: date | None = None
) -> tuple[int, int, int]:
    """Walk every product, snapshot each. Returns (ok, skipped, failed)."""
    products = db.execute(
        select(ShopifyProduct.id).order_by(ShopifyProduct.id)
    ).scalars().all()
    ok = 0
    skipped = 0
    failed = 0
    for pid in products:
        try:
            success, _detail, _n = snapshot_product(db, pid, today=today)
            if success:
                ok += 1
            else:
                # SC misconfig is the only blocker we treat as "skip the rest".
                if "Search Console not configured" in (_detail or ""):
                    logger.warning("SC not configured; aborting snapshot loop")
                    skipped += len(products) - ok
                    break
                skipped += 1
        except Exception:  # noqa: BLE001
            logger.exception("kw history snapshot crashed for product_id=%d", pid)
            failed += 1
    return ok, skipped, failed


def growth_alerts(
    db: Session,
    *,
    days_back: int = 7,
    limit: int = 50,
) -> list[dict]:
    """Compare today vs (today - days_back) per product+keyword. Return the
    most interesting movements, ranked by absolute Δ position (or by clicks
    for new-keyword arrivals)."""
    today = _latest_snapshot_date(db)
    if today is None:
        return []
    then = today - timedelta(days=days_back)

    # Today's rows (one query, indexed)
    today_rows = db.execute(
        select(ProductKeywordHistory).where(
            ProductKeywordHistory.snapshot_date == today
        )
    ).scalars().all()
    if not today_rows:
        return []

    # Find the most recent snapshot row on/before `then` per (product, keyword).
    # Simple O(N) lookup: gather all rows with date <= then for the same
    # (product, keyword) and pick the latest. To keep this cheap in SQL we
    # just pull all relevant comparison rows for the prior 60 days.
    horizon = today - timedelta(days=max(60, days_back * 3))
    prior_rows = db.execute(
        select(ProductKeywordHistory)
        .where(ProductKeywordHistory.snapshot_date >= horizon)
        .where(ProductKeywordHistory.snapshot_date <= then)
    ).scalars().all()
    prior_by_key: dict[tuple[int, str], ProductKeywordHistory] = {}
    for row in prior_rows:
        key = (row.product_id, row.keyword)
        prev = prior_by_key.get(key)
        if prev is None or row.snapshot_date > prev.snapshot_date:
            prior_by_key[key] = row

    # Resolve product titles in one query.
    pids = sorted({r.product_id for r in today_rows})
    title_by_pid: dict[int, str] = {}
    if pids:
        for r in db.execute(
            select(ShopifyProduct.id, ShopifyProduct.title).where(
                ShopifyProduct.id.in_(pids)
            )
        ).all():
            title_by_pid[r.id] = r.title

    alerts: list[dict] = []
    for cur in today_rows:
        key = (cur.product_id, cur.keyword)
        prev = prior_by_key.get(key)
        cur_pos = cur.sc_position or 0
        prev_pos = prev.sc_position if prev else None

        if prev is None:
            # First time we've ever seen this keyword for this product.
            alerts.append({
                "type": "new_keyword",
                "product_id": cur.product_id,
                "product_title": title_by_pid.get(cur.product_id, "—"),
                "keyword": cur.keyword,
                "current_position": cur_pos,
                "previous_position": None,
                "delta": None,
                "clicks": cur.sc_clicks or 0,
                "impressions": cur.sc_impressions or 0,
            })
            continue

        delta = prev_pos - cur_pos if (prev_pos is not None) else None  # lower = better
        if delta is not None and abs(delta) >= MOVEMENT_THRESHOLD:
            kind = "improved" if delta > 0 else "regressed"
            # Special-flag arrivals into top 10 / top 3.
            if cur_pos <= 3 and (prev_pos or 99) > 3:
                kind = "top_3"
            elif cur_pos <= 10 and (prev_pos or 99) > 10:
                kind = "top_10"
            alerts.append({
                "type": kind,
                "product_id": cur.product_id,
                "product_title": title_by_pid.get(cur.product_id, "—"),
                "keyword": cur.keyword,
                "current_position": cur_pos,
                "previous_position": prev_pos,
                "delta": delta,
                "clicks": cur.sc_clicks or 0,
                "impressions": cur.sc_impressions or 0,
            })

    # Ranking: improvements first, by abs(delta) desc; then top arrivals; then new keywords by impressions.
    type_order = {"top_3": 0, "top_10": 1, "improved": 2, "new_keyword": 3, "regressed": 4}
    alerts.sort(
        key=lambda a: (
            type_order.get(a["type"], 9),
            -abs(a["delta"] or 0),
            -(a["impressions"] or 0),
        )
    )
    return alerts[:limit]


def alert_counts_by_type(alerts: Sequence[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for a in alerts:
        out[a["type"]] = out.get(a["type"], 0) + 1
    return out


def _latest_snapshot_date(db: Session) -> date | None:
    return db.scalar(select(func.max(ProductKeywordHistory.snapshot_date)))


def keywords_gained_per_product(
    db: Session, days_back: int = 7
) -> list[dict]:
    """Per-product count of brand-new keywords gained in the window. Used by
    the dashboard 'products on the rise' panel."""
    today = _latest_snapshot_date(db)
    if today is None:
        return []
    then = today - timedelta(days=days_back)
    cur_rows = db.execute(
        select(
            ProductKeywordHistory.product_id, ProductKeywordHistory.keyword
        ).where(ProductKeywordHistory.snapshot_date >= then)
    ).all()
    horizon = today - timedelta(days=max(60, days_back * 4))
    prev_rows = db.execute(
        select(
            ProductKeywordHistory.product_id, ProductKeywordHistory.keyword
        )
        .where(ProductKeywordHistory.snapshot_date >= horizon)
        .where(ProductKeywordHistory.snapshot_date < then)
    ).all()
    prev_pairs: set[tuple[int, str]] = {(r.product_id, r.keyword) for r in prev_rows}
    cur_pairs: set[tuple[int, str]] = {(r.product_id, r.keyword) for r in cur_rows}
    new_pairs = cur_pairs - prev_pairs

    per_product: dict[int, int] = {}
    for pid, _kw in new_pairs:
        per_product[pid] = per_product.get(pid, 0) + 1
    if not per_product:
        return []
    pids = sorted(per_product.keys())
    rows = db.execute(
        select(ShopifyProduct.id, ShopifyProduct.title).where(
            ShopifyProduct.id.in_(pids)
        )
    ).all()
    title_by_pid = {r.id: r.title for r in rows}
    out = [
        {
            "product_id": pid,
            "product_title": title_by_pid.get(pid, "—"),
            "keywords_gained": n,
        }
        for pid, n in per_product.items()
    ]
    out.sort(key=lambda r: -r["keywords_gained"])
    return out
