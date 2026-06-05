"""Customizable analytics dashboard backend (PERF-DETAIL).

Owns three things:
  1. The metric catalog — Core Business metrics (Profit, Ad Spend, CAC) and a
     filterable list of GA4 Analytics metrics by category.
  2. Per-user persistence of the chosen metric set (on users.preferences).
  3. Value computation (current vs previous period), chart series for the
     dual-axis Performance Trends chart, and the four data tables.

Values are sourced from helena_metric_snapshots (platform 'meta_ads',
'instagram', 'email', 'ga4') plus existing Shopify sales data. Anything not yet
wired returns 0 / empty gracefully — the UI and structure are fully present.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gglads.models.campaign import AdCampaign
from gglads.models.helena import MetaAdCampaign, MetricSnapshot
from gglads.models.shopify_product import ShopifyDailySales, ShopifyProduct
from gglads.models.user import User
from gglads.services import user_prefs as prefs_svc

# ---------------------------------------------------------------------------
# Metric catalog
# ---------------------------------------------------------------------------

# Core Business — always available, computed from spend/revenue/customers.
CORE_METRICS: list[dict[str, Any]] = [
    {"key": "profit", "name": "Profit", "icon": "💰",
     "desc": "Revenue minus ad spend over the period.", "unit": "currency", "axis": "left"},
    {"key": "ad_spend", "name": "Ad Spend", "icon": "📣",
     "desc": "Total advertising spend across connected ad platforms.",
     "unit": "currency", "axis": "left"},
    {"key": "cac", "name": "Customer Acquisition Cost", "icon": "🎯",
     "desc": "Ad spend divided by new customers acquired.",
     "unit": "currency", "axis": "left"},
]

# GA4 Analytics — filterable by category chip.
GA4_CATEGORIES = ["All", "Users", "Sessions", "Engagement", "Conversions", "Revenue"]
GA4_METRICS: list[dict[str, Any]] = [
    {"key": "ga4_active_users", "name": "Active Users", "category": "Users",
     "desc": "Distinct users who engaged in the period.", "unit": "count", "axis": "right"},
    {"key": "ga4_new_users", "name": "New Users", "category": "Users",
     "desc": "First-time users.", "unit": "count", "axis": "right"},
    {"key": "ga4_total_users", "name": "Total Users", "category": "Users",
     "desc": "All users in the period.", "unit": "count", "axis": "right"},
    {"key": "ga4_sessions", "name": "Sessions", "category": "Sessions",
     "desc": "Total sessions.", "unit": "count", "axis": "right"},
    {"key": "ga4_engaged_sessions", "name": "Engaged Sessions", "category": "Sessions",
     "desc": "Sessions longer than 10s or with a conversion.", "unit": "count", "axis": "right"},
    {"key": "ga4_sessions_per_user", "name": "Sessions / User", "category": "Sessions",
     "desc": "Average sessions per user.", "unit": "ratio", "axis": "right"},
    {"key": "ga4_engagement_rate", "name": "Engagement Rate", "category": "Engagement",
     "desc": "Share of engaged sessions.", "unit": "percent", "axis": "right"},
    {"key": "ga4_avg_engagement_time", "name": "Avg Engagement Time", "category": "Engagement",
     "desc": "Average engagement time per session (seconds).", "unit": "duration", "axis": "right"},
    {"key": "ga4_bounce_rate", "name": "Bounce Rate", "category": "Engagement",
     "desc": "Share of non-engaged sessions.", "unit": "percent", "axis": "right"},
    {"key": "ga4_views", "name": "Views", "category": "Engagement",
     "desc": "Page and screen views.", "unit": "count", "axis": "right"},
    {"key": "ga4_conversions", "name": "Conversions", "category": "Conversions",
     "desc": "Total conversions.", "unit": "count", "axis": "right"},
    {"key": "ga4_conversion_rate", "name": "Conversion Rate", "category": "Conversions",
     "desc": "Session conversion rate.", "unit": "percent", "axis": "right"},
    {"key": "ga4_key_events", "name": "Key Events", "category": "Conversions",
     "desc": "Count of key events.", "unit": "count", "axis": "right"},
    {"key": "ga4_ecommerce_purchases", "name": "Ecommerce Purchases", "category": "Conversions",
     "desc": "Completed purchases.", "unit": "count", "axis": "right"},
    {"key": "ga4_total_revenue", "name": "Total Revenue", "category": "Revenue",
     "desc": "Total revenue attributed in GA4.", "unit": "currency", "axis": "left"},
    {"key": "ga4_purchase_revenue", "name": "Purchase Revenue", "category": "Revenue",
     "desc": "Revenue from purchases.", "unit": "currency", "axis": "left"},
    {"key": "ga4_arpu", "name": "ARPU", "category": "Revenue",
     "desc": "Average revenue per active user.", "unit": "currency", "axis": "left"},
]

DEFAULT_SELECTED = ["profit", "ad_spend", "cac", "ga4_sessions", "ga4_active_users"]

_BY_KEY: dict[str, dict[str, Any]] = {m["key"]: m for m in (*CORE_METRICS, *GA4_METRICS)}

# Distinct series colors for the chart legend.
SERIES_COLORS = [
    "#7c9cff", "#4ade80", "#fbbf24", "#f87171", "#a78bfa",
    "#22d3ee", "#fb923c", "#f472b6", "#34d399", "#60a5fa",
]


def metric_by_key(key: str) -> dict[str, Any] | None:
    return _BY_KEY.get(key)


def catalog() -> dict[str, Any]:
    return {"core": CORE_METRICS, "ga4": GA4_METRICS, "categories": GA4_CATEGORIES}


# ---------------------------------------------------------------------------
# Per-user selected metric set
# ---------------------------------------------------------------------------

def get_selected(user: User) -> list[str]:
    sel = prefs_svc.load_prefs(user).get("dashboard_metrics")
    if isinstance(sel, list) and sel:
        return [k for k in sel if k in _BY_KEY]
    return list(DEFAULT_SELECTED)


def set_selected(db: Session, user: User, keys: list[str]) -> list[str]:
    clean = [k for k in keys if k in _BY_KEY]
    prefs = prefs_svc.load_prefs(user)
    prefs["dashboard_metrics"] = clean
    prefs_svc.save_prefs(db, user, prefs)
    return clean


def toggle_metric(db: Session, user: User, key: str) -> list[str]:
    if key not in _BY_KEY:
        return get_selected(user)
    sel = get_selected(user)
    if key in sel:
        sel = [k for k in sel if k != key]
    else:
        sel = [*sel, key]
    return set_selected(db, user, sel)


# ---------------------------------------------------------------------------
# Value computation
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(UTC)


def _sum_snapshot(db: Session, platform: str, metric: str, start: datetime, end: datetime) -> float:
    rows = db.scalars(
        select(MetricSnapshot.value)
        .where(MetricSnapshot.platform == platform)
        .where(MetricSnapshot.metric == metric)
        .where(MetricSnapshot.captured_for >= start)
        .where(MetricSnapshot.captured_for < end)
    ).all()
    return float(sum(rows, Decimal(0)))


def _metric_value(db: Session, key: str, start: datetime, end: datetime) -> float:
    """Compute one metric's value over [start, end)."""
    if key.startswith("ga4_"):
        ga_metric = key[len("ga4_"):]
        return _sum_snapshot(db, "ga4", ga_metric, start, end)
    if key == "ad_spend":
        return _sum_snapshot(db, "meta_ads", "spend", start, end) + \
            _sum_snapshot(db, "google_ads", "spend", start, end)
    if key == "profit":
        revenue = _sum_snapshot(db, "meta_ads", "revenue", start, end) + \
            _sum_snapshot(db, "ga4", "total_revenue", start, end)
        spend = _metric_value(db, "ad_spend", start, end)
        return revenue - spend
    if key == "cac":
        spend = _metric_value(db, "ad_spend", start, end)
        new_customers = _sum_snapshot(db, "ga4", "new_users", start, end) or \
            _sum_snapshot(db, "meta_ads", "conversions", start, end)
        return round(spend / new_customers, 2) if new_customers else 0.0
    return 0.0


def _pct_change(current: float, previous: float) -> float | None:
    if previous == 0:
        return None
    return round((current - previous) / abs(previous) * 100, 1)


def format_value(unit: str, value: float) -> str:
    if unit == "currency":
        return f"${value:,.0f}" if abs(value) >= 1000 else f"${value:,.2f}"
    if unit == "percent":
        return f"{value:.1f}%"
    if unit == "duration":
        return f"{value:.0f}s"
    if unit in ("ratio",):
        return f"{value:.2f}"
    return f"{value:,.0f}"


def cards(db: Session, user: User, days: int) -> list[dict[str, Any]]:
    end = _now()
    start = end - timedelta(days=days)
    prev_start = start - timedelta(days=days)
    out: list[dict[str, Any]] = []
    for key in get_selected(user):
        meta = _BY_KEY[key]
        cur = _metric_value(db, key, start, end)
        prev = _metric_value(db, key, prev_start, start)
        out.append({
            **meta,
            "value": cur,
            "previous": prev,
            "formatted": format_value(meta["unit"], cur),
            "pct_change": _pct_change(cur, prev),
        })
    return out


# ---------------------------------------------------------------------------
# Chart series (dual-axis, multi-series)
# ---------------------------------------------------------------------------

def _daily(db: Session, key: str, days: int) -> dict[str, float]:
    """Per-day values for a metric, keyed by ISO date."""
    end = _now()
    start = end - timedelta(days=days)

    def bucket(platform: str, metric: str) -> dict[str, float]:
        rows = db.scalars(
            select(MetricSnapshot)
            .where(MetricSnapshot.platform == platform)
            .where(MetricSnapshot.metric == metric)
            .where(MetricSnapshot.captured_for >= start)
        ).all()
        b: dict[str, float] = defaultdict(float)
        for r in rows:
            b[r.captured_for.date().isoformat()] += float(r.value)
        return b

    if key.startswith("ga4_"):
        return bucket("ga4", key[len("ga4_"):])
    if key == "ad_spend":
        b = bucket("meta_ads", "spend")
        for d, v in bucket("google_ads", "spend").items():
            b[d] = b.get(d, 0.0) + v
        return b
    if key == "profit":
        rev = bucket("meta_ads", "revenue")
        spend = _daily(db, "ad_spend", days)
        keys = set(rev) | set(spend)
        return {d: rev.get(d, 0.0) - spend.get(d, 0.0) for d in keys}
    return {}


def chart_series(db: Session, user: User, days: int) -> dict[str, Any]:
    """Build the dual-axis Performance Trends payload for the selected metrics."""
    end = _now()
    labels = [(end - timedelta(days=days - 1 - i)).date().isoformat() for i in range(days)]
    series = []
    for i, key in enumerate(get_selected(user)):
        meta = _BY_KEY[key]
        daily = _daily(db, key, days)
        points = [round(daily.get(d, 0.0), 2) for d in labels]
        series.append({
            "key": key,
            "name": meta["name"],
            "color": SERIES_COLORS[i % len(SERIES_COLORS)],
            "axis": meta.get("axis", "left"),
            "unit": meta["unit"],
            "points": points,
        })
    return {"labels": labels, "series": series}


# ---------------------------------------------------------------------------
# Data tables
# ---------------------------------------------------------------------------

PAGE_SIZE = 5


def _favicon(domain: str) -> str:
    return f"https://www.google.com/s2/favicons?domain={domain}&sz=32"


def _table(title: str, key: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    for i, r in enumerate(rows):
        r["rank"] = i + 1
        r["pct_change"] = _pct_change(r.get("current", 0.0), r.get("previous", 0.0))
    return {
        "title": title, "key": key, "rows": rows,
        "page_size": PAGE_SIZE, "total": len(rows),
        "remaining": max(0, len(rows) - PAGE_SIZE),
    }


def table_source_medium(db: Session, days: int) -> dict[str, Any]:
    """Session Source/Medium — sourced from Shopify daily sales channels."""
    end = _now().date()
    start = end - timedelta(days=days)
    prev_start = start - timedelta(days=days)
    label_map = {
        "web": ("Online Store / organic", "shopify.com"),
        "shop": ("Shop app / referral", "shop.app"),
    }

    def window(s, e) -> dict[str, float]:
        rows = db.execute(
            select(ShopifyDailySales.channel, func.coalesce(func.sum(ShopifyDailySales.revenue), 0))
            .where(ShopifyDailySales.product_id.is_(None))
            .where(ShopifyDailySales.snapshot_date >= s)
            .where(ShopifyDailySales.snapshot_date < e)
            .group_by(ShopifyDailySales.channel)
        ).all()
        return {c: float(v) for c, v in rows}

    cur = window(start, end)
    prev = window(prev_start, start)
    rows = []
    for ch in sorted(cur, key=lambda c: cur[c], reverse=True):
        label, domain = label_map.get(ch, (ch, "shopify.com"))
        rows.append({"label": label, "favicon": _favicon(domain),
                     "current": round(cur.get(ch, 0.0), 2),
                     "previous": round(prev.get(ch, 0.0), 2), "unit": "currency"})
    return _table("Session Source / Medium", "source_medium", rows)


def table_landing_pages(db: Session, days: int) -> dict[str, Any]:
    """Top Landing Pages — top products by units (proxy for product pages)."""
    end = _now().date()
    start = end - timedelta(days=days)
    prev_start = start - timedelta(days=days)

    def window(s, e) -> dict[int, float]:
        rows = db.execute(
            select(ShopifyDailySales.product_id, func.coalesce(func.sum(ShopifyDailySales.units), 0))
            .where(ShopifyDailySales.product_id.is_not(None))
            .where(ShopifyDailySales.snapshot_date >= s)
            .where(ShopifyDailySales.snapshot_date < e)
            .group_by(ShopifyDailySales.product_id)
        ).all()
        return {pid: float(u) for pid, u in rows}

    cur = window(start, end)
    prev = window(prev_start, start)
    top = sorted(cur, key=lambda p: cur[p], reverse=True)[:25]
    rows = []
    for pid in top:
        prod = db.get(ShopifyProduct, pid)
        if prod is None:
            continue
        rows.append({"label": f"/products/{prod.handle}", "favicon": _favicon("shopify.com"),
                     "current": round(cur.get(pid, 0.0)), "previous": round(prev.get(pid, 0.0)),
                     "unit": "count"})
    return _table("Top Landing Pages", "landing_pages", rows)


def table_google_campaigns(db: Session, days: int) -> dict[str, Any]:
    end = _now()
    start = end - timedelta(days=days)
    prev_start = start - timedelta(days=days)
    camps = db.scalars(
        select(AdCampaign).order_by(AdCampaign.daily_budget_cents.desc()).limit(25)
    ).all()
    rows = []
    for c in camps:
        cur = _sum_snapshot(db, "google_ads", "spend", start, end) if c.google_ads_campaign_id else 0.0
        prev = _sum_snapshot(db, "google_ads", "spend", prev_start, start) if c.google_ads_campaign_id else 0.0
        rows.append({"label": c.name, "favicon": _favicon("google.com"),
                     "current": round(cur, 2) or round(c.daily_budget_cents / 100, 2),
                     "previous": round(prev, 2), "unit": "currency"})
    return _table("Top Google Campaigns", "google_campaigns", rows)


def table_meta_campaigns(db: Session, days: int) -> dict[str, Any]:
    end = _now()
    start = end - timedelta(days=days)
    prev_start = start - timedelta(days=days)

    def window(s, e) -> dict[int, float]:
        rows = db.scalars(
            select(MetricSnapshot)
            .where(MetricSnapshot.platform == "meta_ads")
            .where(MetricSnapshot.entity_type == "campaign")
            .where(MetricSnapshot.metric == "spend")
            .where(MetricSnapshot.captured_for >= s)
            .where(MetricSnapshot.captured_for < e)
        ).all()
        b: dict[int, float] = defaultdict(float)
        for r in rows:
            if r.entity_id is not None:
                b[r.entity_id] += float(r.value)
        return b

    cur = window(start, end)
    prev = window(prev_start, start)
    ids = sorted(set(cur) | set(prev), key=lambda i: cur.get(i, 0.0), reverse=True)
    rows = []
    for cid in ids[:25]:
        camp = db.get(MetaAdCampaign, cid)
        rows.append({"label": camp.name if camp else f"Campaign {cid}",
                     "favicon": _favicon("facebook.com"),
                     "current": round(cur.get(cid, 0.0), 2),
                     "previous": round(prev.get(cid, 0.0), 2), "unit": "currency"})
    return _table("Top Meta Campaigns", "meta_campaigns", rows)


def all_tables(db: Session, days: int) -> list[dict[str, Any]]:
    return [
        table_source_medium(db, days),
        table_landing_pages(db, days),
        table_google_campaigns(db, days),
        table_meta_campaigns(db, days),
    ]
