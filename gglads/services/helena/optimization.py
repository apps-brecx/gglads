"""Spend-optimization module.

Analyzes per-campaign metrics returned by the provider and recommends where to
increase or decrease spend: flag high-ROAS campaigns to scale, low performers
to cut. Recommendations surface both in chat (via the digest) and on the
dashboard. Recommendations are advisory — any budget change still goes through
the approval-gated task queue.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from gglads.models.helena import MetaAdCampaign
from gglads.services.helena import analytics as analytics_svc

# Tunable thresholds.
HIGH_ROAS = 2.5
LOW_ROAS = 1.0
MIN_SPEND_FOR_JUDGEMENT = 20.0  # dollars — ignore campaigns with too little data


def recommendations(db: Session, days: int = 14) -> list[dict[str, Any]]:
    rows = analytics_svc.per_campaign(db, days=days)
    recs: list[dict[str, Any]] = []
    for r in rows:
        if r["spend"] < MIN_SPEND_FOR_JUDGEMENT:
            continue
        camp = db.get(MetaAdCampaign, r["campaign_id"])
        name = camp.name if camp else f"Campaign {r['campaign_id']}"
        roas = r["roas"]
        if roas >= HIGH_ROAS:
            recs.append({
                "campaign_id": r["campaign_id"],
                "action": "scale",
                "severity": "good",
                "headline": f"Scale '{name}': ROAS {roas:.1f}x — raise budget ~30%.",
                "suggested_budget_cents": int(((camp.budget_cents if camp else 0)) * 1.3),
                "metrics": r,
            })
        elif roas < LOW_ROAS and r["spend"] > 0:
            recs.append({
                "campaign_id": r["campaign_id"],
                "action": "cut",
                "severity": "warn",
                "headline": f"Cut '{name}': ROAS {roas:.1f}x — reduce budget or pause.",
                "suggested_budget_cents": int(((camp.budget_cents if camp else 0)) * 0.5),
                "metrics": r,
            })
    # Best performers first.
    recs.sort(key=lambda x: (x["action"] != "scale", -x["metrics"]["roas"]))
    return recs
