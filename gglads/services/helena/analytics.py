"""Analytics: ingest normalized metrics from any provider, aggregate them for
the dashboard, and produce the recurring performance-digest text.

Metrics arrive as a flat list of dicts (platform, entity_type, entity_id,
metric, value, captured_for) from ProviderResult.metrics — identical whether
the browser agent or the API produced them.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from gglads.models.helena import MetricSnapshot

logger = logging.getLogger("gglads.helena.analytics")


def _now() -> datetime:
    return datetime.now(UTC)


def ingest_metrics(db: Session, rows: list[dict[str, Any]]) -> int:
    """Persist provider metric rows. Returns count written."""
    written = 0
    for r in rows:
        try:
            value = Decimal(str(r.get("value", 0)))
        except (InvalidOperation, TypeError):
            continue
        captured_for = r.get("captured_for")
        if isinstance(captured_for, str):
            try:
                captured_for = datetime.fromisoformat(captured_for)
            except ValueError:
                captured_for = _now()
        elif not isinstance(captured_for, datetime):
            captured_for = _now()
        db.add(
            MetricSnapshot(
                platform=r.get("platform", "unknown"),
                entity_type=r.get("entity_type", "account"),
                entity_id=r.get("entity_id"),
                metric=r.get("metric", "unknown"),
                value=value,
                captured_for=captured_for,
            )
        )
        written += 1
    if written:
        db.commit()
    return written


def _sum(db: Session, platform: str, metric: str, since: datetime) -> Decimal:
    rows = db.scalars(
        select(MetricSnapshot.value)
        .where(MetricSnapshot.platform == platform)
        .where(MetricSnapshot.metric == metric)
        .where(MetricSnapshot.captured_for >= since)
    ).all()
    return sum(rows, Decimal(0))


def topline(db: Session, days: int = 30) -> dict[str, Any]:
    """Top-line cards across all platforms for the dashboard."""
    since = _now() - timedelta(days=days)
    meta_spend = _sum(db, "meta_ads", "spend", since)
    meta_clicks = _sum(db, "meta_ads", "clicks", since)
    meta_impr = _sum(db, "meta_ads", "impressions", since)
    meta_conv = _sum(db, "meta_ads", "conversions", since)
    meta_rev = _sum(db, "meta_ads", "revenue", since)
    ig_reach = _sum(db, "instagram", "reach", since)
    ig_eng = _sum(db, "instagram", "engagement", since)
    email_opens = _sum(db, "email", "opens", since)
    email_clicks = _sum(db, "email", "clicks", since)

    def ratio(a: Decimal, b: Decimal) -> float:
        return float(a / b) if b else 0.0

    return {
        "days": days,
        "meta": {
            "spend": float(meta_spend),
            "impressions": float(meta_impr),
            "clicks": float(meta_clicks),
            "conversions": float(meta_conv),
            "revenue": float(meta_rev),
            "cpc": round(ratio(meta_spend, meta_clicks), 2),
            "cpm": round(ratio(meta_spend * 1000, meta_impr), 2),
            "roas": round(ratio(meta_rev, meta_spend), 2),
        },
        "instagram": {
            "reach": float(ig_reach),
            "engagement": float(ig_eng),
            "engagement_rate": round(ratio(ig_eng, ig_reach) * 100, 2),
        },
        "email": {
            "opens": float(email_opens),
            "clicks": float(email_clicks),
            "ctr": round(ratio(email_clicks, email_opens) * 100, 2),
        },
    }


def per_campaign(db: Session, days: int = 30) -> list[dict[str, Any]]:
    """Per-campaign metric rollup for the dashboard table + optimizer."""
    since = _now() - timedelta(days=days)
    rows = db.scalars(
        select(MetricSnapshot)
        .where(MetricSnapshot.platform == "meta_ads")
        .where(MetricSnapshot.entity_type == "campaign")
        .where(MetricSnapshot.captured_for >= since)
    ).all()
    agg: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        if r.entity_id is None:
            continue
        agg[r.entity_id][r.metric] += float(r.value)
    out = []
    for cid, m in agg.items():
        spend = m.get("spend", 0.0)
        clicks = m.get("clicks", 0.0)
        rev = m.get("revenue", 0.0)
        out.append({
            "campaign_id": cid,
            "spend": round(spend, 2),
            "impressions": round(m.get("impressions", 0.0)),
            "clicks": round(clicks),
            "conversions": round(m.get("conversions", 0.0)),
            "revenue": round(rev, 2),
            "cpc": round(spend / clicks, 2) if clicks else 0.0,
            "roas": round(rev / spend, 2) if spend else 0.0,
        })
    return sorted(out, key=lambda x: x["spend"], reverse=True)


def timeseries(db: Session, platform: str, metric: str, days: int = 30) -> list[dict[str, Any]]:
    since = _now() - timedelta(days=days)
    rows = db.scalars(
        select(MetricSnapshot)
        .where(MetricSnapshot.platform == platform)
        .where(MetricSnapshot.metric == metric)
        .where(MetricSnapshot.captured_for >= since)
        .order_by(MetricSnapshot.captured_for)
    ).all()
    bucket: dict[str, float] = defaultdict(float)
    for r in rows:
        bucket[r.captured_for.date().isoformat()] += float(r.value)
    return [{"date": d, "value": round(v, 2)} for d, v in sorted(bucket.items())]


def digest_text(db: Session, days: int = 7) -> str:
    t = topline(db, days=days)
    m, ig, em = t["meta"], t["instagram"], t["email"]
    lines = [
        f"Performance digest — last {days} days",
        "",
        f"Meta Ads: ${m['spend']:.0f} spend · {m['clicks']:.0f} clicks · "
        f"CPC ${m['cpc']:.2f} · ROAS {m['roas']:.2f}x · {m['conversions']:.0f} conv",
        f"Instagram: {ig['reach']:.0f} reach · {ig['engagement']:.0f} engagement · "
        f"{ig['engagement_rate']:.1f}% eng rate",
        f"Email: {em['opens']:.0f} opens · {em['clicks']:.0f} clicks · {em['ctr']:.1f}% CTR",
    ]
    from gglads.services.helena import optimization as opt
    recs = opt.recommendations(db, days=days)
    if recs:
        lines.append("")
        lines.append("Recommendations:")
        for r in recs[:5]:
            lines.append(f"• {r['headline']}")
    return "\n".join(lines)
