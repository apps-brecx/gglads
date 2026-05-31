"""AI-generated SEO drafts for a product: title, meta, description, bullets,
and per-image alt text. Drafts are saved as pending; the user approves/rejects."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from gglads.models.product_keywords import ProductKeyword
from gglads.models.shopify_product import (
    ProductSeoDraft,
    ShopifyCollection,
    ShopifyProduct,
    ShopifyProductCollection,
    ShopifyProductImage,
)
from gglads.services import claude as claude_svc

logger = logging.getLogger("gglads.seo")


SEO_SYSTEM = """You are a senior e-commerce SEO copywriter. Given a product and the \
keywords the brand wants to rank for, generate SEO assets that are honest, accurate, and \
naturally incorporate target keywords. Never invent claims, certifications, ingredients, \
or specifications that aren't in the brief.

Output a single JSON object exactly in this format (no commentary):

{
  "seo_title": "string, ≤60 chars, includes the primary keyword early",
  "meta_description": "string, ≤155 chars, compelling and includes 1-2 keywords",
  "description_html": "valid HTML: 2-4 short paragraphs and 1 <ul> with 4-6 <li> features, \
total length 600-1200 chars. Honest, accurate, brand-voice consistent.",
  "bullets": ["5 short, scannable feature/benefit bullets", "...", "...", "...", "..."]
}

Hard rules:
- Keep seo_title ≤ 60 characters
- Keep meta_description between 120 and 155 characters
- Use the brand's voice if provided in training
- Do not stuff keywords — use them naturally
- Do not make claims the brief doesn't support
"""


IMAGE_ALT_SYSTEM = """You are an e-commerce SEO copywriter writing image alt text. \
Generate ONE descriptive alt-text string for the product image described, naturally \
including 1-2 relevant search keywords from the list provided.

Output a single JSON object exactly:
{ "alt": "the alt text, ≤125 chars" }

Rules:
- Describe what's visible (the product, context, angle, color, material if known)
- Include 1-2 keywords from the list naturally — do not stuff
- ≤125 chars
- No marketing phrases like "best", "premium", "amazing"
- No URLs, no quotes around the value
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
        c = candidate[i]
        if c == "{":
            depth += 1
        elif c == "}":
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


def _product_brief(db: Session, product: ShopifyProduct) -> str:
    collection_titles = db.execute(
        select(ShopifyCollection.title)
        .join(
            ShopifyProductCollection,
            ShopifyProductCollection.collection_id == ShopifyCollection.id,
        )
        .where(ShopifyProductCollection.product_id == product.id)
    ).scalars().all()
    desc = (product.description_html or "")[:2000]
    return (
        f"Product title: {product.title}\n"
        f"Vendor: {product.vendor or '—'}\n"
        f"Product type: {product.product_type or '—'}\n"
        f"Price: ${product.price_min or '—'}{(' - $' + str(product.price_max)) if product.price_max and product.price_max != product.price_min else ''}\n"
        f"Status: {product.status}\n"
        f"Collections: {', '.join(collection_titles) or '—'}\n"
        f"Current description: {desc}\n"
        f"Current SEO title: {product.seo_title or '(empty)'}\n"
        f"Current meta description: {product.seo_meta_description or '(empty)'}\n"
    )


def _top_keywords(db: Session, product_id: int, limit: int = 20) -> list[str]:
    """Approved Primary + Secondary keywords, plus organic search-console terms."""
    rows = db.execute(
        select(ProductKeyword)
        .where(ProductKeyword.product_id == product_id)
        .where(ProductKeyword.bucket.in_(("primary", "secondary")))
        .order_by(ProductKeyword.relevance_score.desc().nullslast())
        .limit(limit)
    ).scalars().all()
    return [r.keyword for r in rows]


def _replace_pending(db: Session, product_id: int, field: str, image_id: int | None = None) -> None:
    q = (
        select(ProductSeoDraft)
        .where(ProductSeoDraft.product_id == product_id)
        .where(ProductSeoDraft.field == field)
        .where(ProductSeoDraft.status == "pending")
    )
    if image_id is not None:
        q = q.where(ProductSeoDraft.image_id == image_id)
    pending = db.execute(q).scalars().all()
    for d in pending:
        d.status = "superseded"


def generate_seo_drafts(
    db: Session, product_id: int
) -> tuple[bool, str]:
    product = db.get(ShopifyProduct, product_id)
    if product is None:
        return False, "Product not found."

    keywords = _top_keywords(db, product_id)
    if not keywords:
        return (
            False,
            "No approved keywords yet. Run keyword research on the Ads tab and approve some Primary/Secondary keywords first.",
        )

    user_msg = (
        f"{_product_brief(db, product)}\n"
        f"Target keywords (use 2-4 naturally): {', '.join(keywords[:12])}\n"
    )
    text, err = claude_svc.chat(
        db, system=SEO_SYSTEM, user_message=user_msg, max_tokens=4000
    )
    if err or not text:
        return False, err or "Claude returned no text."
    data = _extract_json(text)
    if not data:
        return False, "Claude response was not parseable JSON."

    # Persist drafts (one per field)
    for field, value in [
        ("seo_title", data.get("seo_title")),
        ("meta_description", data.get("meta_description")),
        ("description", data.get("description_html")),
        ("bullets", json.dumps(data.get("bullets", []))),
    ]:
        if not value:
            continue
        _replace_pending(db, product_id, field)
        db.add(
            ProductSeoDraft(
                product_id=product_id,
                field=field,
                suggested_value=str(value),
                status="pending",
            )
        )
    db.commit()
    return True, "Generated suggestions for SEO title, meta description, product description, and bullets."


def generate_image_alt(
    db: Session, product_id: int, image_id: int | None = None
) -> tuple[bool, str]:
    """Generate alt text for one image (image_id) or all images on a product."""
    product = db.get(ShopifyProduct, product_id)
    if product is None:
        return False, "Product not found."
    keywords = _top_keywords(db, product_id)
    if not keywords:
        # Fall back to broad descriptors so we don't block alt generation
        keywords = [product.product_type or product.title]

    if image_id is not None:
        images = db.execute(
            select(ShopifyProductImage)
            .where(ShopifyProductImage.product_id == product_id)
            .where(ShopifyProductImage.id == image_id)
        ).scalars().all()
    else:
        images = db.execute(
            select(ShopifyProductImage)
            .where(ShopifyProductImage.product_id == product_id)
            .order_by(ShopifyProductImage.position)
        ).scalars().all()
    if not images:
        return False, "No product images to describe."

    successes = 0
    for img in images:
        user_msg = (
            f"Product: {product.title}\n"
            f"Product type: {product.product_type or '—'}\n"
            f"Vendor: {product.vendor or '—'}\n"
            f"Position in gallery: {img.position} (0 = main / featured)\n"
            f"Image URL: {img.url}\n"
            f"Current alt text: {img.alt_text or '(empty)'}\n"
            f"Target keywords: {', '.join(keywords[:8])}\n"
        )
        text, err = claude_svc.chat(
            db, system=IMAGE_ALT_SYSTEM, user_message=user_msg, max_tokens=400
        )
        if err or not text:
            logger.warning("Alt text generation failed for image %s: %s", img.id, err)
            continue
        data = _extract_json(text)
        if not data or not data.get("alt"):
            continue
        alt = str(data["alt"]).strip()[:125]
        _replace_pending(db, product_id, "image_alt", image_id=img.id)
        db.add(
            ProductSeoDraft(
                product_id=product_id,
                field="image_alt",
                image_id=img.id,
                suggested_value=alt,
                status="pending",
            )
        )
        successes += 1
    db.commit()
    if successes == 0:
        return False, "No alt text suggestions could be generated."
    return True, f"Generated alt text for {successes} of {len(images)} image(s)."


def approve_draft(
    db: Session, draft_id: int, user_id: int | None
) -> tuple[bool, str]:
    draft = db.get(ProductSeoDraft, draft_id)
    if draft is None or draft.status != "pending":
        return False, "Draft not found or already actioned."
    draft.status = "approved"
    draft.approved_at = datetime.now(timezone.utc)
    draft.approved_by_user_id = user_id
    db.commit()
    return True, f"Approved {draft.field}."


def reject_draft(db: Session, draft_id: int) -> tuple[bool, str]:
    draft = db.get(ProductSeoDraft, draft_id)
    if draft is None or draft.status != "pending":
        return False, "Draft not found or already actioned."
    draft.status = "rejected"
    db.commit()
    return True, f"Rejected {draft.field}."
