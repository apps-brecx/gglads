"""Campaign CRUD + helpers.

Scope-agnostic — works for both product and collection campaigns. Pushing
to Google Ads (MutationService) is a later phase; for now these functions
manage the gglads-side state only.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from gglads.models.campaign import AdCampaign, AdCampaignKeyword
from gglads.models.product_keywords import ProductKeyword
from gglads.models.shopify_product import ShopifyCollection, ShopifyProduct
from gglads.services import integrations as integrations_svc

logger = logging.getLogger("gglads.campaigns")


AI_ACTIONS = [
    ("pause_low_ctr", "Pause keywords with low CTR"),
    ("raise_bid_high_conv", "Raise bid on high-converting keywords"),
    ("lower_bid_low_conv", "Lower bid on low-converting keywords"),
    ("add_negative", "Add negative keywords from waste search terms"),
    ("promote_search_term", "Promote winning search terms to exact-match keywords"),
    ("adjust_daily_budget", "Adjust daily budget within configured caps"),
    ("pause_campaign", "Pause campaign if hitting CPA ceiling"),
]


BID_STRATEGIES = [
    ("maximize_conversions", "Maximize conversions"),
    ("target_cpa", "Target CPA"),
    ("maximize_clicks", "Maximize clicks"),
    ("manual_cpc", "Manual CPC"),
]


def _store_url(db: Session) -> str:
    cfg = integrations_svc.get_config(db, "shopify")
    domain = (cfg.get("store_domain") or "").strip().rstrip("/")
    if not domain:
        return ""
    if not domain.startswith("http"):
        domain = f"https://{domain}"
    return domain


def _default_landing_url(db: Session, scope_type: str, scope_id: int) -> str:
    if scope_type == "product":
        p = db.get(ShopifyProduct, scope_id)
        if p is not None and p.handle:
            base = _store_url(db)
            return f"{base}/products/{p.handle}" if base else p.handle
    if scope_type == "collection":
        c = db.get(ShopifyCollection, scope_id)
        if c is not None and c.handle:
            base = _store_url(db)
            return f"{base}/collections/{c.handle}" if base else c.handle
    return ""


def _default_name(db: Session, scope_type: str, scope_id: int) -> str:
    if scope_type == "product":
        p = db.get(ShopifyProduct, scope_id)
        if p is not None:
            return f"{p.title} — Search"
    if scope_type == "collection":
        c = db.get(ShopifyCollection, scope_id)
        if c is not None:
            return f"{c.title} — Search"
    return "New campaign"


def create_draft(
    db: Session, scope_type: str, scope_id: int, user_id: int | None
) -> tuple[bool, str, int | None]:
    if scope_type not in ("product", "collection"):
        return False, f"Unknown scope: {scope_type}", None

    if scope_type == "product":
        target = db.get(ShopifyProduct, scope_id)
    else:
        target = db.get(ShopifyCollection, scope_id)
    if target is None:
        return False, f"{scope_type} not found.", None

    campaign = AdCampaign(
        scope_type=scope_type,
        product_id=scope_id if scope_type == "product" else None,
        collection_id=scope_id if scope_type == "collection" else None,
        name=_default_name(db, scope_type, scope_id),
        status="draft",
        daily_budget_cents=2000,  # $20/day default
        bid_strategy="maximize_conversions",
        landing_page_url=_default_landing_url(db, scope_type, scope_id),
        ai_managed=False,
        ai_min_data_clicks=20,
        ai_actions_allowed_json=json.dumps([a[0] for a in AI_ACTIONS[:4]]),
        created_by_user_id=user_id,
    )
    db.add(campaign)
    db.commit()
    db.refresh(campaign)

    # For product campaigns, copy Primary/Secondary product keywords as a starting set.
    if scope_type == "product":
        product_kws = db.execute(
            select(ProductKeyword)
            .where(ProductKeyword.product_id == scope_id)
            .where(ProductKeyword.bucket.in_(("primary", "secondary")))
        ).scalars().all()
        for pk in product_kws:
            db.add(
                AdCampaignKeyword(
                    campaign_id=campaign.id,
                    text=pk.keyword,
                    match_type=pk.match_type or "phrase",
                    is_negative=False,
                )
            )
        # negatives too
        negatives = db.execute(
            select(ProductKeyword)
            .where(ProductKeyword.product_id == scope_id)
            .where(ProductKeyword.bucket == "negative")
        ).scalars().all()
        for pk in negatives:
            db.add(
                AdCampaignKeyword(
                    campaign_id=campaign.id,
                    text=pk.keyword,
                    match_type=pk.match_type or "phrase",
                    is_negative=True,
                )
            )
        db.commit()

    return True, "Campaign draft created.", campaign.id


def update_basics(
    db: Session,
    campaign_id: int,
    *,
    name: str | None = None,
    status: str | None = None,
    daily_budget_cents: int | None = None,
    bid_strategy: str | None = None,
    target_cpa_cents: int | None = None,
    landing_page_url: str | None = None,
) -> tuple[bool, str]:
    c = db.get(AdCampaign, campaign_id)
    if c is None:
        return False, "Campaign not found."
    if name is not None:
        c.name = name[:255]
    if status in ("draft", "active", "paused", "archived"):
        c.status = status
    if daily_budget_cents is not None:
        c.daily_budget_cents = max(0, int(daily_budget_cents))
    if bid_strategy in {b[0] for b in BID_STRATEGIES}:
        c.bid_strategy = bid_strategy
    if target_cpa_cents is not None:
        c.target_cpa_cents = max(0, int(target_cpa_cents)) if target_cpa_cents else None
    if landing_page_url is not None:
        c.landing_page_url = landing_page_url[:2000] or None
    c.updated_at = datetime.now(timezone.utc)
    db.commit()
    return True, "Saved."


def update_ai_settings(
    db: Session,
    campaign_id: int,
    *,
    ai_managed: bool,
    ai_target_cpa_cents: int | None,
    ai_max_daily_budget_cents: int | None,
    ai_min_daily_budget_cents: int | None,
    ai_min_data_clicks: int,
    actions_allowed: list[str],
) -> tuple[bool, str]:
    c = db.get(AdCampaign, campaign_id)
    if c is None:
        return False, "Campaign not found."
    valid_actions = {a[0] for a in AI_ACTIONS}
    actions = [a for a in actions_allowed if a in valid_actions]
    c.ai_managed = bool(ai_managed)
    c.ai_target_cpa_cents = ai_target_cpa_cents if ai_target_cpa_cents else None
    c.ai_max_daily_budget_cents = (
        ai_max_daily_budget_cents if ai_max_daily_budget_cents else None
    )
    c.ai_min_daily_budget_cents = (
        ai_min_daily_budget_cents if ai_min_daily_budget_cents else None
    )
    c.ai_min_data_clicks = max(0, int(ai_min_data_clicks or 0))
    c.ai_actions_allowed_json = json.dumps(actions)
    c.updated_at = datetime.now(timezone.utc)
    db.commit()
    return True, "AI settings saved."


def add_keyword(
    db: Session,
    campaign_id: int,
    text: str,
    match_type: str = "phrase",
    is_negative: bool = False,
) -> tuple[bool, str]:
    c = db.get(AdCampaign, campaign_id)
    if c is None:
        return False, "Campaign not found."
    text = (text or "").strip().lower()
    if not text:
        return False, "Empty keyword."
    if match_type not in ("exact", "phrase", "broad"):
        match_type = "phrase"
    # Dedupe
    existing = db.scalar(
        select(AdCampaignKeyword)
        .where(AdCampaignKeyword.campaign_id == campaign_id)
        .where(AdCampaignKeyword.text == text)
        .where(AdCampaignKeyword.is_negative == is_negative)
    )
    if existing is not None:
        return False, "Keyword already in this campaign."
    db.add(
        AdCampaignKeyword(
            campaign_id=campaign_id,
            text=text[:255],
            match_type=match_type,
            is_negative=is_negative,
        )
    )
    db.commit()
    return True, "Added."


def remove_keyword(
    db: Session, campaign_id: int, keyword_id: int
) -> tuple[bool, str]:
    kw = db.get(AdCampaignKeyword, keyword_id)
    if kw is None or kw.campaign_id != campaign_id:
        return False, "Keyword not found."
    db.delete(kw)
    db.commit()
    return True, "Removed."


def update_ad_copy(
    db: Session,
    campaign_id: int,
    headlines: list[str],
    descriptions: list[str],
) -> tuple[bool, str]:
    c = db.get(AdCampaign, campaign_id)
    if c is None:
        return False, "Campaign not found."
    headlines = [h.strip()[:30] for h in headlines if h.strip()][:15]
    descriptions = [d.strip()[:90] for d in descriptions if d.strip()][:4]
    c.headlines_json = json.dumps(headlines)
    c.descriptions_json = json.dumps(descriptions)
    c.updated_at = datetime.now(timezone.utc)
    db.commit()
    return True, "Ad copy saved."


def parse_actions(c: AdCampaign) -> list[str]:
    if not c.ai_actions_allowed_json:
        return []
    try:
        v = json.loads(c.ai_actions_allowed_json)
        return [a for a in v if isinstance(a, str)]
    except (ValueError, TypeError):
        return []


def parse_list(text: str | None) -> list[str]:
    if not text:
        return []
    try:
        v = json.loads(text)
        if isinstance(v, list):
            return [str(x) for x in v]
    except (ValueError, TypeError):
        pass
    return []


def delete_campaign(db: Session, campaign_id: int) -> tuple[bool, str]:
    c = db.get(AdCampaign, campaign_id)
    if c is None:
        return False, "Campaign not found."
    db.delete(c)
    db.commit()
    return True, "Campaign deleted."


def scope_label(db: Session, c: AdCampaign) -> dict[str, Any]:
    """For the master list — show what this campaign targets."""
    if c.scope_type == "product" and c.product_id:
        p = db.get(ShopifyProduct, c.product_id)
        return {
            "kind": "product",
            "title": p.title if p else "(deleted product)",
            "url": f"/products/{c.product_id}" if p else None,
        }
    if c.scope_type == "collection" and c.collection_id:
        col = db.get(ShopifyCollection, c.collection_id)
        return {
            "kind": "collection",
            "title": col.title if col else "(deleted collection)",
            "url": f"/products/collection/{col.handle}" if col else None,
        }
    return {"kind": "unknown", "title": "—", "url": None}
