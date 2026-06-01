"""Collection-level helpers: list, detail, SEO save, organic queries.

Mirrors the pattern of services/seo_generation.py for products but at the
collection level. SEO save goes straight onto the collection row (no draft
workflow yet — collections are lower-volume than products, so a per-edit
approval queue would be overkill).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gglads.models.product_keywords import ProductKeyword
from gglads.models.shopify_product import (
    ShopifyCollection,
    ShopifyProduct,
    ShopifyProductCollection,
)
from gglads.services import claude as claude_svc
from gglads.services import integrations as integrations_svc
from gglads.services import search_console as sc_svc

logger = logging.getLogger("gglads.collections")


def list_collections(db: Session) -> list[dict]:
    """All collections with product counts and a quick SEO completeness flag."""
    rows = db.execute(
        select(ShopifyCollection).order_by(ShopifyCollection.title)
    ).scalars().all()
    out: list[dict] = []
    for c in rows:
        seo_filled = bool((c.seo_title or "").strip()) and bool(
            (c.seo_meta_description or "").strip()
        )
        out.append({
            "id": c.id,
            "title": c.title,
            "handle": c.handle,
            "description": c.description,
            "image_url": c.image_url,
            "product_count": c.product_count,
            "seo_title": c.seo_title,
            "seo_meta_description": c.seo_meta_description,
            "seo_filled": seo_filled,
            "seo_updated_at": c.seo_updated_at,
        })
    return out


def get_collection(db: Session, handle: str) -> ShopifyCollection | None:
    return db.scalar(
        select(ShopifyCollection).where(ShopifyCollection.handle == handle)
    )


def products_in_collection(db: Session, collection_id: int) -> list[ShopifyProduct]:
    return list(
        db.execute(
            select(ShopifyProduct)
            .join(
                ShopifyProductCollection,
                ShopifyProductCollection.product_id == ShopifyProduct.id,
            )
            .where(ShopifyProductCollection.collection_id == collection_id)
            .order_by(ShopifyProduct.net_sales_90d.desc().nullslast(), ShopifyProduct.title)
        ).scalars().all()
    )


def page_url_for_collection(db: Session, handle: str) -> str | None:
    """Public collection URL — used as the `page` filter for Search Console."""
    cfg = integrations_svc.get_config(db, "google_search_console")
    site_url = (cfg.get("site_url") or "").strip()
    if not site_url:
        # Fallback to the Shopify store domain so the page still has a URL
        # shown even when Search Console isn't wired up yet.
        shopify_cfg = integrations_svc.get_config(db, "shopify")
        domain = (shopify_cfg.get("store_domain") or "").strip().rstrip("/")
        if not domain:
            return None
        if not domain.startswith("http"):
            domain = f"https://{domain}"
        return f"{domain}/collections/{handle}"
    if site_url.startswith("sc-domain:"):
        domain = site_url[len("sc-domain:"):]
        return f"https://{domain}/collections/{handle}"
    return f"{site_url.rstrip('/')}/collections/{handle}"


def organic_queries(
    db: Session, handle: str, days: int = 90, row_limit: int = 50
) -> tuple[list[dict] | None, str | None]:
    """Query Search Console for organic queries that landed on this collection's URL."""
    url = page_url_for_collection(db, handle)
    if not url:
        return None, "No store URL configured."
    return sc_svc.get_queries_for_page(db, url, days=days, row_limit=row_limit)


def update_seo(
    db: Session,
    collection_id: int,
    *,
    seo_title: str | None,
    seo_meta_description: str | None,
    description: str | None,
) -> tuple[bool, str]:
    c = db.get(ShopifyCollection, collection_id)
    if c is None:
        return False, "Collection not found."
    if seo_title is not None:
        c.seo_title = (seo_title or "").strip()[:255] or None
    if seo_meta_description is not None:
        c.seo_meta_description = (seo_meta_description or "").strip() or None
    if description is not None:
        c.description = (description or "").strip() or None
    c.seo_updated_at = datetime.now(timezone.utc)
    db.commit()
    return True, "Collection SEO saved."


# ---------------------------------------------------------------------------
# AI generation — title / meta description / body HTML for the collection
# ---------------------------------------------------------------------------

_COLLECTION_SEO_SYSTEM = """You are a senior e-commerce SEO copywriter writing for a Shopify \
collection page. Goal: rank for the keywords listed in the brief, and convert browsers into clickers.

Rules:
- Title: ≤ 60 chars, includes the primary keyword naturally, mentions the brand only if helpful.
- Meta description: 140-155 chars, includes 1-2 secondary keywords naturally, ends with a soft CTA.
- Body description: 120-220 words of HTML (<p>, <h2>, <ul>, <li> only). Uses the primary keyword \
in the opening paragraph and 1-2 H2s. Reads like real copy, not a keyword pile. No emoji, no \
unsupported claims (no awards, no ingredient invention, no 'best ever' fluff).

Output JSON only, exactly:
{
  "seo_title": "string ≤60 chars",
  "seo_meta_description": "string 140-155 chars",
  "description_html": "<p>…</p><h2>…</h2><ul>…</ul>",
  "rationale": "1 short sentence on the SEO strategy"
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


def _organic_seed_keywords(
    db: Session, handle: str, limit: int = 12
) -> list[str]:
    """Pull the top organic queries that already landed on this collection's URL —
    the strongest signal for what we should target. If Search Console isn't wired
    up or returns nothing, return an empty list."""
    rows, err = organic_queries(db, handle, days=90, row_limit=50)
    if err or not rows:
        return []
    # Order by impressions desc, then position asc (better position = lower number)
    rows.sort(key=lambda r: (-r.get("impressions", 0), r.get("position", 99)))
    return [r["query"] for r in rows[:limit] if r.get("query")]


def _related_product_keywords(
    db: Session, collection_id: int, limit: int = 12
) -> list[str]:
    """Highest-scored ProductKeyword rows across all products in the collection
    — useful when Search Console has no data yet."""
    rows = db.execute(
        select(ProductKeyword.keyword)
        .join(
            ShopifyProductCollection,
            ShopifyProductCollection.product_id == ProductKeyword.product_id,
        )
        .where(ShopifyProductCollection.collection_id == collection_id)
        .order_by(ProductKeyword.relevance_score.desc().nullslast())
        .limit(limit)
    ).scalars().all()
    # Deduplicate while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for k in rows:
        k = k.lower().strip()
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def generate_seo(
    db: Session, collection_id: int
) -> tuple[bool, str, dict | None]:
    """Ask Claude for title / meta description / body HTML based on the
    collection's organic queries and member products' keywords."""
    c = db.get(ShopifyCollection, collection_id)
    if c is None:
        return False, "Collection not found.", None

    organic = _organic_seed_keywords(db, c.handle)
    product_kws = _related_product_keywords(db, collection_id)
    if not organic and not product_kws:
        return (
            False,
            "Nothing to target yet — run keyword research on at least one product in this "
            "collection, or wire up Search Console so we have organic queries to anchor on.",
            None,
        )

    products = products_in_collection(db, collection_id)
    sample_products = "\n".join(
        f"  - {p.title} (${p.price_min or '—'})"
        for p in products[:12]
    ) or "  (no linked products)"

    brief = (
        f"COLLECTION: {c.title}\n"
        f"Handle: /collections/{c.handle}\n"
        f"Current title: {c.title}\n"
        f"Current SEO title: {c.seo_title or '(empty)'}\n"
        f"Current meta description: {c.seo_meta_description or '(empty)'}\n"
        f"Current description (truncated): "
        f"{(c.description or '(empty)')[:600]}\n"
        f"\nProducts in this collection (sample):\n{sample_products}\n"
        f"\nOrganic queries this URL already ranks for (Search Console, top 12):\n  "
        + (", ".join(organic) if organic else "(none yet)")
        + f"\n\nHigh-relevance keywords from member products (top {len(product_kws)}):\n  "
        + (", ".join(product_kws) if product_kws else "(none)")
        + "\n\nGenerate SEO copy now. Prioritize organic queries you have data for."
    )

    text, err = claude_svc.chat(
        db, system=_COLLECTION_SEO_SYSTEM, user_message=brief, max_tokens=2000
    )
    if err or not text:
        return False, err or "Claude returned no text.", None
    data = _extract_json(text)
    if not data:
        return False, "Claude reply was not parseable JSON.", None

    seo_title = str(data.get("seo_title") or "").strip()[:255]
    meta_desc = str(data.get("seo_meta_description") or "").strip()
    body_html = str(data.get("description_html") or "").strip()
    rationale = str(data.get("rationale") or "").strip()[:500]

    c.seo_title = seo_title or None
    c.seo_meta_description = meta_desc or None
    c.description = body_html or None
    c.seo_updated_at = datetime.now(timezone.utc)
    db.commit()

    summary = (
        f"Generated SEO for \"{c.title}\". "
        f"Used {len(organic)} organic query/queries and "
        f"{len(product_kws)} product keyword(s)."
    )
    if rationale:
        summary += f" Strategy: {rationale}"
    return True, summary, {
        "seo_title": seo_title,
        "seo_meta_description": meta_desc,
        "description_html": body_html,
        "rationale": rationale,
        "organic_seeds": organic,
        "product_seeds": product_kws,
    }
