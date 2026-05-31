"""AI generation of Responsive Search Ad copy per ad group.

Tunes for quality score 10:
- Keywords naturally in headlines (the page also has them per coverage)
- Compelling, brand-voice copy
- Strong calls to action in descriptions
- Display paths reinforce keyword

Also detects which of the ad group's keywords are MISSING from the product's
SEO surfaces (title / meta title / meta desc / description / image alts) and
records that on product_keywords.seo_targets so the next SEO generation must
include them — closing the loop between ads and SEO.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from gglads.models.campaign import AdCampaign, AdCampaignKeyword, AdGroup
from gglads.models.product_keywords import ProductKeyword
from gglads.models.shopify_product import ShopifyProduct
from gglads.services import claude as claude_svc
from gglads.services import keyword_placement as kw_place_svc
from gglads.services import seo_chat as chat_svc

logger = logging.getLogger("gglads.ad_copy")


AD_COPY_SYSTEM = """You are a senior Google Ads copywriter targeting a 10/10 ad \
relevance quality score. Write a single Responsive Search Ad for ONE ad group.

Rules:
- Up to 15 headlines, each ≤ 30 characters (count carefully — include spaces)
- Up to 4 descriptions, each ≤ 90 characters
- Include the primary keyword (or close variants) in at least 3 headlines naturally
- Include a clear call to action in at least one headline and at least one description
- No marketing fluff that has no specific claim
- No emoji
- No exclamation points unless absolutely earned
- No claims the product brief doesn't support (do not invent ingredients, awards, etc.)
- Display paths (path1, path2) reinforce the keyword/theme, ≤ 15 chars each
- Use the brand's voice from global chat context if provided

Output JSON only, exactly this shape:
{
  "headlines": ["string ≤30 chars", "..."],
  "descriptions": ["string ≤90 chars", "..."],
  "path1": "string ≤15 chars",
  "path2": "string ≤15 chars",
  "quality_notes": "1-2 sentences explaining how this hits a 10 QS"
}
"""


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fence.group(1) if fence else text
    start = candidate.find("{")
    if start == -1:
        return None
    depth = 0
    end = -1
    for i in range(start, len(candidate)):
        ch = candidate[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end == -1:
        return None
    try:
        return json.loads(candidate[start:end])
    except json.JSONDecodeError:
        return None


def _missing_from_seo_surfaces(
    db: Session, product_id: int, keywords: list[str]
) -> dict[str, list[str]]:
    """Return {keyword: [missing_field, …]} for keywords missing from any SEO surface."""
    coverage = kw_place_svc.coverage_for_product(db, product_id)
    out: dict[str, list[str]] = {}
    for kw in keywords:
        cov = coverage.get(kw.lower(), {})
        missing = [field for field in kw_place_svc.SEO_FIELDS if not cov.get(field)]
        if missing:
            out[kw] = missing
    return out


def _push_to_seo_targets(
    db: Session, product_id: int, missing_by_kw: dict[str, list[str]]
) -> int:
    """Update product_keywords.seo_targets so the next SEO generation must include
    these keywords in the listed fields. Returns count of keywords updated."""
    if not missing_by_kw:
        return 0
    updated = 0
    for kw_text, fields in missing_by_kw.items():
        pk = db.scalar(
            select(ProductKeyword)
            .where(ProductKeyword.product_id == product_id)
            .where(ProductKeyword.keyword == kw_text.lower())
        )
        if pk is None:
            continue
        existing = kw_place_svc.parse_seo_targets(pk.seo_targets)
        merged = sorted(set(existing) | set(fields))
        pk.seo_targets = json.dumps(merged)
        pk.updated_at = datetime.now(timezone.utc)
        updated += 1
    db.commit()
    return updated


def generate_for_ad_group(
    db: Session, campaign_id: int, ad_group_id: int
) -> tuple[bool, str, dict | None]:
    campaign = db.get(AdCampaign, campaign_id)
    ad_group = db.get(AdGroup, ad_group_id)
    if campaign is None or ad_group is None or ad_group.campaign_id != campaign_id:
        return False, "Campaign or ad group not found.", None

    positive_kws = db.execute(
        select(AdCampaignKeyword)
        .where(AdCampaignKeyword.ad_group_id == ad_group_id)
        .where(AdCampaignKeyword.is_negative.is_(False))
        .order_by(AdCampaignKeyword.id)
    ).scalars().all()
    keyword_texts = [k.text for k in positive_kws]
    if not keyword_texts:
        return False, "Ad group has no positive keywords yet. Add some first.", None

    product = (
        db.get(ShopifyProduct, campaign.product_id) if campaign.product_id else None
    )
    product_brief = ""
    if product is not None:
        product_brief = (
            f"Product title: {product.title}\n"
            f"Vendor: {product.vendor or '—'}\n"
            f"Product type: {product.product_type or '—'}\n"
            f"Price: ${product.price_min or '—'}\n"
            f"Description excerpt: {(product.description_html or '')[:1500]}\n"
        )
    else:
        product_brief = f"Campaign scope: collection · landing {campaign.landing_page_url or '—'}\n"

    # Pull chat context (product + global) so brand voice carries over
    chat_lines = "  (no prior chat)"
    if product is not None:
        rows = chat_svc.list_context_for_product(
            db, product.id, topics=("general", "seo", "keywords")
        )
    else:
        rows = chat_svc.list_messages(db, None, topic="general")
    if rows:
        chat_lines = "\n".join(
            f"  [{('GLOBAL' if m.product_id is None else 'product')}/{m.role}] {m.content[:300]}"
            for m in rows[-20:]
        )

    user_msg = (
        f"AD GROUP: {ad_group.name}\n"
        f"Match type: {ad_group.match_type}\n"
        f"Campaign name: {campaign.name}\n"
        f"Landing URL: {campaign.landing_page_url or '—'}\n"
        f"\n"
        f"Keywords (your ad must serve these well):\n"
        f"  {', '.join(keyword_texts[:30])}\n"
        f"\n"
        f"{product_brief}\n"
        f"Brand/user chat context (apply these notes):\n{chat_lines}\n"
        f"\n"
        f"Write the RSA copy now."
    )
    text, err = claude_svc.chat(
        db, system=AD_COPY_SYSTEM, user_message=user_msg, max_tokens=2500
    )
    if err or not text:
        return False, err or "Claude returned no text.", None
    data = _extract_json(text)
    if not data:
        return False, "Claude response was not parseable JSON.", None

    headlines = [str(h)[:30] for h in (data.get("headlines") or []) if str(h).strip()][:15]
    descriptions = [str(d)[:90] for d in (data.get("descriptions") or []) if str(d).strip()][:4]
    path1 = str(data.get("path1") or "").strip()[:15]
    path2 = str(data.get("path2") or "").strip()[:15]

    ad_group.headlines_json = json.dumps(headlines)
    ad_group.descriptions_json = json.dumps(descriptions)
    ad_group.path1 = path1 or None
    ad_group.path2 = path2 or None
    ad_group.updated_at = datetime.now(timezone.utc)
    db.commit()

    seo_pushed_count = 0
    missing_by_kw: dict[str, list[str]] = {}
    if product is not None:
        missing_by_kw = _missing_from_seo_surfaces(db, product.id, keyword_texts)
        seo_pushed_count = _push_to_seo_targets(db, product.id, missing_by_kw)

    summary = (
        f"Wrote {len(headlines)} headlines and {len(descriptions)} descriptions."
    )
    if seo_pushed_count:
        summary += (
            f" {seo_pushed_count} keyword(s) are missing from this product's SEO "
            "surfaces — they've been queued for the next SEO generation. "
            "Visit the SEO & Content tab to apply them."
        )
    if data.get("quality_notes"):
        summary += f" Notes: {data['quality_notes']}"
    return True, summary, {
        "headlines": headlines,
        "descriptions": descriptions,
        "path1": path1,
        "path2": path2,
        "missing_seo_by_keyword": missing_by_kw,
        "seo_pushed_count": seo_pushed_count,
    }
