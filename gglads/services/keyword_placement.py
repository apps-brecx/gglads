"""Master-keyword helpers: compute coverage (where the keyword appears in the
product's SEO surfaces) and push keywords into SEO targets / ads buckets."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from gglads.models.product_keywords import ProductKeyword
from gglads.models.shopify_product import (
    ShopifyProduct,
    ShopifyProductImage,
)
from gglads.services import claude as claude_svc

logger = logging.getLogger("gglads.kw_place")

# Valid placement targets the user can request
SEO_FIELDS = ("title", "meta_title", "meta_description", "description", "image_alts")
BUCKETS = ("primary", "secondary", "negative", "ignore", "unsorted")


def _haystack(value: str | None) -> str:
    return (value or "").lower()


def coverage_for_product(db: Session, product_id: int) -> dict[str, dict]:
    """Return {keyword_lower: {title, meta_title, meta_description, description,
    image_alts}} all booleans."""
    p = db.get(ShopifyProduct, product_id)
    if p is None:
        return {}
    images = db.execute(
        select(ShopifyProductImage.alt_text).where(
            ShopifyProductImage.product_id == product_id
        )
    ).scalars().all()
    alts_blob = " ".join((alt or "") for alt in images).lower()

    fields = {
        "title": _haystack(p.title),
        "meta_title": _haystack(p.seo_title),
        "meta_description": _haystack(p.seo_meta_description),
        "description": _haystack(p.description_html),
        "image_alts": alts_blob,
    }

    kws = db.execute(
        select(ProductKeyword.keyword).where(ProductKeyword.product_id == product_id)
    ).scalars().all()
    out: dict[str, dict] = {}
    for kw in kws:
        k = kw.lower().strip()
        out[k] = {fname: (k in haystack) for fname, haystack in fields.items()}
    return out


def parse_seo_targets(s: str | None) -> list[str]:
    if not s:
        return []
    try:
        v = json.loads(s)
        if isinstance(v, list):
            return [x for x in v if isinstance(x, str) and x in SEO_FIELDS]
    except (ValueError, TypeError):
        pass
    return []


def push_to_seo(
    db: Session, product_id: int, keyword_id: int, fields: list[str]
) -> tuple[bool, str]:
    """Add the given SEO fields to the keyword's seo_targets list (additive)."""
    kw = db.get(ProductKeyword, keyword_id)
    if kw is None or kw.product_id != product_id:
        return False, "Keyword not found."
    valid = [f for f in fields if f in SEO_FIELDS]
    if not valid:
        return False, "No valid SEO fields chosen."
    existing = parse_seo_targets(kw.seo_targets)
    merged = sorted(set(existing) | set(valid))
    kw.seo_targets = json.dumps(merged)
    kw.updated_at = datetime.now(timezone.utc)
    db.commit()
    return True, f"Marked \"{kw.keyword}\" for: {', '.join(valid)}."


def clear_seo_targets(
    db: Session, product_id: int, keyword_id: int
) -> tuple[bool, str]:
    kw = db.get(ProductKeyword, keyword_id)
    if kw is None or kw.product_id != product_id:
        return False, "Keyword not found."
    kw.seo_targets = None
    kw.updated_at = datetime.now(timezone.utc)
    db.commit()
    return True, "Cleared SEO targets."


def set_bucket(
    db: Session, product_id: int, keyword_id: int, bucket: str
) -> tuple[bool, str]:
    if bucket not in BUCKETS:
        return False, "Invalid bucket."
    kw = db.get(ProductKeyword, keyword_id)
    if kw is None or kw.product_id != product_id:
        return False, "Keyword not found."
    kw.bucket = bucket
    kw.updated_at = datetime.now(timezone.utc)
    db.commit()
    return True, f"Moved \"{kw.keyword}\" to {bucket}."


_PLACEMENT_SYSTEM = """Given a product and a keyword the brand wants to use, recommend \
the BEST placements. Consider keyword length (long-tail fits in description; short \
fits in title), search intent, and where it would help SEO the most.

Reply JSON only:
{
  "seo_fields": ["title", "meta_title", "meta_description", "description", "image_alts"],
  "ads_bucket": "primary" | "secondary" | "negative",
  "rationale": "one short sentence"
}

Only include fields that are actually a good fit. Empty list is OK if none.
"""


def ai_suggest_placement(
    db: Session, product_id: int, keyword_id: int
) -> tuple[bool, str, dict | None]:
    kw = db.get(ProductKeyword, keyword_id)
    if kw is None or kw.product_id != product_id:
        return False, "Keyword not found.", None
    p = db.get(ShopifyProduct, product_id)
    if p is None:
        return False, "Product not found.", None

    cov = coverage_for_product(db, product_id).get(kw.keyword.lower(), {})
    cov_str = ", ".join(f"{k}={'in' if v else 'missing'}" for k, v in cov.items())

    user_msg = (
        f"Product: {p.title} ({p.product_type or '—'}, vendor {p.vendor or '—'})\n"
        f"Current title: {p.title}\n"
        f"Current SEO title: {p.seo_title or '(empty)'}\n"
        f"Current meta description: {p.seo_meta_description or '(empty)'}\n"
        f"Keyword: \"{kw.keyword}\" ({len(kw.keyword)} chars)\n"
        f"Keyword intent: {kw.intent or '—'} / funnel: {kw.funnel or '—'} / "
        f"score: {kw.relevance_score or '—'}\n"
        f"Source: {kw.source}\n"
        f"Coverage right now: {cov_str}\n"
        f"Current ads bucket: {kw.bucket}\n"
        "Where should this keyword go?"
    )
    text, err = claude_svc.chat(
        db, system=_PLACEMENT_SYSTEM, user_message=user_msg, max_tokens=400
    )
    if err or not text:
        return False, err or "No response from Claude.", None

    # Reuse the simple JSON extractor pattern
    import re

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return False, "Claude reply was not parseable JSON.", None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return False, "Claude reply was not parseable JSON.", None
    seo_fields = [f for f in (data.get("seo_fields") or []) if f in SEO_FIELDS]
    bucket = data.get("ads_bucket") if data.get("ads_bucket") in BUCKETS else None
    rationale = (data.get("rationale") or "")[:300]
    return True, rationale or "Suggestion ready.", {
        "seo_fields": seo_fields,
        "ads_bucket": bucket,
        "rationale": rationale,
    }
