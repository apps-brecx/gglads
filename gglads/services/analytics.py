"""Sales analytics — daily rollups, growth deltas, top movers.

Reads from shopify_daily_sales (filled by services.shopify._sync_orders). All
queries respect the channel allowlist (web + shop) implicitly because that's
the only data we ingest.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gglads.models.shopify_product import ShopifyDailySales, ShopifyProduct

# Channels we display + label them for the UI.
CHANNEL_LABELS: dict[str, str] = {
    "web": "Online Store",
    "shop": "Shop app",
}


def _today() -> date:
    """Wrapped so tests can monkey-patch."""
    return date.today()


def _window(days: int) -> tuple[date, date]:
    """Inclusive [start, end] for the most recent `days` days, ending today."""
    end = _today()
    start = end - timedelta(days=days - 1)
    return start, end


def _prev_window(days: int) -> tuple[date, date]:
    """The window of the same length immediately preceding `days`."""
    end = _today() - timedelta(days=days)
    start = end - timedelta(days=days - 1)
    return start, end


def _date_range(start: date, end: date) -> list[date]:
    out: list[date] = []
    d = start
    while d <= end:
        out.append(d)
        d += timedelta(days=1)
    return out


def daily_totals(
    db: Session,
    days: int,
    channels: Iterable[str] | None = None,
    product_id: int | None = None,
) -> list[dict]:
    """One row per day in the window. Channel rollup is summed when multiple
    channels are selected. Days with zero sales come back with zeros (no
    gaps) so the chart x-axis is continuous.

    product_id=None → store-wide (uses the product_id IS NULL rollup rows).
    product_id=N    → that one product (uses its per-product rollup rows).
    """
    start, end = _window(days)
    stmt = (
        select(
            ShopifyDailySales.snapshot_date.label("day"),
            func.sum(ShopifyDailySales.orders).label("orders"),
            func.sum(ShopifyDailySales.units).label("units"),
            func.sum(ShopifyDailySales.revenue).label("revenue"),
            func.sum(ShopifyDailySales.unique_customers).label("customers"),
        )
        .where(ShopifyDailySales.snapshot_date >= start)
        .where(ShopifyDailySales.snapshot_date <= end)
        .group_by(ShopifyDailySales.snapshot_date)
        .order_by(ShopifyDailySales.snapshot_date)
    )
    if product_id is None:
        stmt = stmt.where(ShopifyDailySales.product_id.is_(None))
    else:
        stmt = stmt.where(ShopifyDailySales.product_id == product_id)
    if channels:
        stmt = stmt.where(ShopifyDailySales.channel.in_(list(channels)))

    by_day: dict[date, dict] = {}
    for row in db.execute(stmt).all():
        by_day[row.day] = {
            "day": row.day,
            "orders": int(row.orders or 0),
            "units": int(row.units or 0),
            "revenue": Decimal(row.revenue or 0),
            "customers": int(row.customers or 0),
        }
    # Fill zero-gap days so the chart has continuous x-axis.
    out: list[dict] = []
    for d in _date_range(start, end):
        out.append(
            by_day.get(d, {
                "day": d,
                "orders": 0,
                "units": 0,
                "revenue": Decimal(0),
                "customers": 0,
            })
        )
    return out


def channel_split(db: Session, days: int) -> list[dict]:
    """Revenue + units totals per channel for the window."""
    start, end = _window(days)
    stmt = (
        select(
            ShopifyDailySales.channel,
            func.sum(ShopifyDailySales.orders).label("orders"),
            func.sum(ShopifyDailySales.units).label("units"),
            func.sum(ShopifyDailySales.revenue).label("revenue"),
            func.sum(ShopifyDailySales.unique_customers).label("customers"),
        )
        .where(ShopifyDailySales.product_id.is_(None))
        .where(ShopifyDailySales.snapshot_date >= start)
        .where(ShopifyDailySales.snapshot_date <= end)
        .group_by(ShopifyDailySales.channel)
    )
    out: list[dict] = []
    for row in db.execute(stmt).all():
        out.append({
            "channel": row.channel,
            "label": CHANNEL_LABELS.get(row.channel, row.channel),
            "orders": int(row.orders or 0),
            "units": int(row.units or 0),
            "revenue": Decimal(row.revenue or 0),
            "customers": int(row.customers or 0),
        })
    # Stable order so the donut palette never reshuffles.
    out.sort(key=lambda r: CHANNEL_LABELS.get(r["channel"], r["channel"]))
    return out


def growth_summary(
    db: Session, days: int, channels: Iterable[str] | None = None
) -> dict:
    """Totals for the current window vs the prior window of equal length.

    Each metric comes back as {"current": x, "previous": y, "delta_pct": p}
    where delta_pct is None if previous == 0 (avoid div-by-zero).
    """
    cur = _aggregate(db, *_window(days), channels)
    prev = _aggregate(db, *_prev_window(days), channels)

    def delta(a, b) -> float | None:
        a_f = float(a)
        b_f = float(b)
        if b_f == 0:
            return None
        return (a_f - b_f) / b_f * 100.0

    return {
        "revenue":   {"current": cur["revenue"],   "previous": prev["revenue"],   "delta_pct": delta(cur["revenue"], prev["revenue"])},
        "orders":    {"current": cur["orders"],    "previous": prev["orders"],    "delta_pct": delta(cur["orders"], prev["orders"])},
        "units":     {"current": cur["units"],     "previous": prev["units"],     "delta_pct": delta(cur["units"], prev["units"])},
        "customers": {"current": cur["customers"], "previous": prev["customers"], "delta_pct": delta(cur["customers"], prev["customers"])},
    }


def _aggregate(
    db: Session,
    start: date,
    end: date,
    channels: Iterable[str] | None,
) -> dict:
    stmt = (
        select(
            func.coalesce(func.sum(ShopifyDailySales.orders), 0).label("orders"),
            func.coalesce(func.sum(ShopifyDailySales.units), 0).label("units"),
            func.coalesce(func.sum(ShopifyDailySales.revenue), 0).label("revenue"),
            func.coalesce(func.sum(ShopifyDailySales.unique_customers), 0).label("customers"),
        )
        .where(ShopifyDailySales.product_id.is_(None))
        .where(ShopifyDailySales.snapshot_date >= start)
        .where(ShopifyDailySales.snapshot_date <= end)
    )
    if channels:
        stmt = stmt.where(ShopifyDailySales.channel.in_(list(channels)))
    row = db.execute(stmt).one()
    return {
        "orders": int(row.orders or 0),
        "units": int(row.units or 0),
        "revenue": Decimal(row.revenue or 0),
        "customers": int(row.customers or 0),
    }


def top_movers(
    db: Session, days: int, limit: int = 8
) -> list[dict]:
    """Products ranked by absolute revenue delta vs the prior window.
    Returns the title, current/prev revenue + units, and percent change."""
    cur_start, cur_end = _window(days)
    prev_start, prev_end = _prev_window(days)

    def per_product(start: date, end: date) -> dict[int, dict]:
        stmt = (
            select(
                ShopifyDailySales.product_id,
                func.sum(ShopifyDailySales.units).label("units"),
                func.sum(ShopifyDailySales.revenue).label("revenue"),
                func.sum(ShopifyDailySales.orders).label("orders"),
            )
            .where(ShopifyDailySales.product_id.is_not(None))
            .where(ShopifyDailySales.snapshot_date >= start)
            .where(ShopifyDailySales.snapshot_date <= end)
            .group_by(ShopifyDailySales.product_id)
        )
        return {
            r.product_id: {
                "units": int(r.units or 0),
                "revenue": Decimal(r.revenue or 0),
                "orders": int(r.orders or 0),
            }
            for r in db.execute(stmt).all()
        }

    cur = per_product(cur_start, cur_end)
    prev = per_product(prev_start, prev_end)
    pids = set(cur.keys()) | set(prev.keys())
    rows: list[dict] = []
    for pid in pids:
        c = cur.get(pid, {"units": 0, "revenue": Decimal(0), "orders": 0})
        p = prev.get(pid, {"units": 0, "revenue": Decimal(0), "orders": 0})
        prod = db.get(ShopifyProduct, pid)
        if prod is None:
            continue
        delta_rev = c["revenue"] - p["revenue"]
        if p["revenue"] > 0:
            delta_pct: float | None = float(delta_rev) / float(p["revenue"]) * 100.0
        else:
            delta_pct = None
        rows.append({
            "product_id": pid,
            "title": prod.title,
            "image_url": prod.image_url,
            "current_revenue": c["revenue"],
            "previous_revenue": p["revenue"],
            "delta_revenue": delta_rev,
            "delta_pct": delta_pct,
            "current_units": c["units"],
            "current_orders": c["orders"],
        })
    rows.sort(key=lambda r: float(r["delta_revenue"]), reverse=True)
    return rows[:limit]


def product_sparkline(db: Session, product_id: int, days: int) -> list[Decimal]:
    """Daily revenue series for one product (no gap-fill — caller can pad)."""
    start, end = _window(days)
    stmt = (
        select(
            ShopifyDailySales.snapshot_date.label("day"),
            func.sum(ShopifyDailySales.revenue).label("revenue"),
        )
        .where(ShopifyDailySales.product_id == product_id)
        .where(ShopifyDailySales.snapshot_date >= start)
        .where(ShopifyDailySales.snapshot_date <= end)
        .group_by(ShopifyDailySales.snapshot_date)
        .order_by(ShopifyDailySales.snapshot_date)
    )
    by_day: dict[date, Decimal] = {
        r.day: Decimal(r.revenue or 0) for r in db.execute(stmt).all()
    }
    return [by_day.get(d, Decimal(0)) for d in _date_range(start, end)]


def latest_sync_date(db: Session) -> date | None:
    """Most recent date we have any rollup row for. None if table is empty."""
    return db.scalar(select(func.max(ShopifyDailySales.snapshot_date)))


def per_product_totals_in_window(
    db: Session, days: int
) -> dict[int, dict]:
    """Sum orders / units / revenue / customers per product over the last
    `days` days. Used by the products CSV export.

    Returns {product_id: {orders, units, revenue, customers}}. Products with
    no rows in the window are absent from the dict (caller should default to 0)."""
    start, end = _window(days)
    stmt = (
        select(
            ShopifyDailySales.product_id,
            func.sum(ShopifyDailySales.orders).label("orders"),
            func.sum(ShopifyDailySales.units).label("units"),
            func.sum(ShopifyDailySales.revenue).label("revenue"),
            func.sum(ShopifyDailySales.unique_customers).label("customers"),
        )
        .where(ShopifyDailySales.product_id.is_not(None))
        .where(ShopifyDailySales.snapshot_date >= start)
        .where(ShopifyDailySales.snapshot_date <= end)
        .group_by(ShopifyDailySales.product_id)
    )
    out: dict[int, dict] = {}
    for r in db.execute(stmt).all():
        out[int(r.product_id)] = {
            "orders": int(r.orders or 0),
            "units": int(r.units or 0),
            "revenue": Decimal(r.revenue or 0),
            "customers": int(r.customers or 0),
        }
    return out
