"""Campaign CRUD + helpers — ad-group-aware.

Pushing to Google Ads (MutationService) is a later phase; for now these
functions manage gglads-side state only.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from gglads.models.campaign import AdCampaign, AdCampaignKeyword, AdGroup
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


MATCH_TYPES = ("exact", "phrase", "broad")
MATCH_TYPE_LABELS = {"exact": "Exact", "phrase": "Phrase", "broad": "Broad"}


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


def _pushed_keywords_for_product(db: Session, product_id: int) -> list[ProductKeyword]:
    """Only keywords the user has actually pushed to ads via the Keywords-page
    Push popover (bucket = primary or secondary). Negatives loaded separately."""
    return list(
        db.execute(
            select(ProductKeyword)
            .where(ProductKeyword.product_id == product_id)
            .where(ProductKeyword.bucket.in_(("primary", "secondary")))
            .order_by(ProductKeyword.relevance_score.desc().nullslast())
        ).scalars().all()
    )


def _negative_keywords_for_product(db: Session, product_id: int) -> list[ProductKeyword]:
    return list(
        db.execute(
            select(ProductKeyword)
            .where(ProductKeyword.product_id == product_id)
            .where(ProductKeyword.bucket == "negative")
        ).scalars().all()
    )


def create_draft(
    db: Session,
    scope_type: str,
    scope_id: int,
    user_id: int | None,
    name: str | None = None,
    match_types: Iterable[str] = MATCH_TYPES,
) -> tuple[bool, str, int | None]:
    if scope_type not in ("product", "collection"):
        return False, f"Unknown scope: {scope_type}", None
    chosen_match_types = [m for m in match_types if m in MATCH_TYPES]
    if not chosen_match_types:
        return False, "Pick at least one match type (exact / phrase / broad).", None

    if scope_type == "product":
        target = db.get(ShopifyProduct, scope_id)
    else:
        target = db.get(ShopifyCollection, scope_id)
    if target is None:
        return False, f"{scope_type} not found.", None

    final_name = (name or _default_name(db, scope_type, scope_id)).strip()[:255]

    campaign = AdCampaign(
        scope_type=scope_type,
        product_id=scope_id if scope_type == "product" else None,
        collection_id=scope_id if scope_type == "collection" else None,
        name=final_name,
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

    # Create one ad group per selected match type
    ad_groups: list[AdGroup] = []
    for mt in chosen_match_types:
        ag = AdGroup(
            campaign_id=campaign.id,
            name=f"{final_name} — {MATCH_TYPE_LABELS[mt]}",
            match_type=mt,
        )
        db.add(ag)
        ad_groups.append(ag)
    db.commit()
    for ag in ad_groups:
        db.refresh(ag)

    # For product campaigns: seed each ad group with the product's PUSHED-TO-ADS
    # keywords (bucket = primary or secondary). Each gets the group's match type.
    if scope_type == "product":
        pushed = _pushed_keywords_for_product(db, scope_id)
        for ag in ad_groups:
            for pk in pushed:
                db.add(
                    AdCampaignKeyword(
                        campaign_id=campaign.id,
                        ad_group_id=ag.id,
                        text=pk.keyword,
                        match_type=ag.match_type,
                        is_negative=False,
                    )
                )
        # Negatives → attach to the FIRST ad group only (Google Ads supports
        # both ad-group and campaign-level negatives; we keep ad-group for
        # simplicity, user can move them as needed)
        if ad_groups:
            for nk in _negative_keywords_for_product(db, scope_id):
                db.add(
                    AdCampaignKeyword(
                        campaign_id=campaign.id,
                        ad_group_id=ad_groups[0].id,
                        text=nk.keyword,
                        match_type=ad_groups[0].match_type,
                        is_negative=True,
                    )
                )
        db.commit()

    return True, "Campaign + ad groups created.", campaign.id


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
    ad_group_id: int,
    text: str,
    is_negative: bool = False,
) -> tuple[bool, str]:
    c = db.get(AdCampaign, campaign_id)
    ag = db.get(AdGroup, ad_group_id)
    if c is None or ag is None or ag.campaign_id != campaign_id:
        return False, "Campaign or ad group not found."
    text = (text or "").strip().lower()
    if not text:
        return False, "Empty keyword."
    existing = db.scalar(
        select(AdCampaignKeyword)
        .where(AdCampaignKeyword.ad_group_id == ad_group_id)
        .where(AdCampaignKeyword.text == text)
        .where(AdCampaignKeyword.is_negative == is_negative)
    )
    if existing is not None:
        return False, "Keyword already in this ad group."
    db.add(
        AdCampaignKeyword(
            campaign_id=campaign_id,
            ad_group_id=ad_group_id,
            text=text[:255],
            match_type=ag.match_type,
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
    ad_group_id: int,
    headlines: list[str],
    descriptions: list[str],
    path1: str = "",
    path2: str = "",
) -> tuple[bool, str]:
    ag = db.get(AdGroup, ad_group_id)
    if ag is None or ag.campaign_id != campaign_id:
        return False, "Ad group not found."
    headlines = [h.strip()[:30] for h in headlines if h.strip()][:15]
    descriptions = [d.strip()[:90] for d in descriptions if d.strip()][:4]
    ag.headlines_json = json.dumps(headlines)
    ag.descriptions_json = json.dumps(descriptions)
    ag.path1 = (path1 or "").strip()[:15] or None
    ag.path2 = (path2 or "").strip()[:15] or None
    ag.updated_at = datetime.now(timezone.utc)
    db.commit()
    return True, "Ad copy saved."


def delete_ad_group(
    db: Session, campaign_id: int, ad_group_id: int
) -> tuple[bool, str]:
    ag = db.get(AdGroup, ad_group_id)
    if ag is None or ag.campaign_id != campaign_id:
        return False, "Ad group not found."
    db.delete(ag)
    db.commit()
    return True, "Ad group deleted."


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


def campaigns_for_product(db: Session, product_id: int) -> list[AdCampaign]:
    return list(
        db.execute(
            select(AdCampaign)
            .where(AdCampaign.product_id == product_id)
            .order_by(AdCampaign.updated_at.desc())
        ).scalars().all()
    )


def ad_groups_for_campaign(db: Session, campaign_id: int) -> list[AdGroup]:
    return list(
        db.execute(
            select(AdGroup)
            .where(AdGroup.campaign_id == campaign_id)
            .order_by(AdGroup.match_type)
        ).scalars().all()
    )


def keywords_for_ad_group(
    db: Session, ad_group_id: int
) -> tuple[list[AdCampaignKeyword], list[AdCampaignKeyword]]:
    rows = list(
        db.execute(
            select(AdCampaignKeyword)
            .where(AdCampaignKeyword.ad_group_id == ad_group_id)
            .order_by(AdCampaignKeyword.is_negative, AdCampaignKeyword.text)
        ).scalars().all()
    )
    positives = [k for k in rows if not k.is_negative]
    negatives = [k for k in rows if k.is_negative]
    return positives, negatives
