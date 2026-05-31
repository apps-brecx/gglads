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

from gglads.models.product_chat import ProductChatMessage
from gglads.models.product_keywords import ProductKeyword
from gglads.models.shopify_product import (
    ProductSeoDraft,
    ShopifyCollection,
    ShopifyProduct,
    ShopifyProductCollection,
    ShopifyProductImage,
)
from gglads.services import claude as claude_svc
from gglads.services import shopify_push as shopify_push_svc

logger = logging.getLogger("gglads.seo")


SEO_SYSTEM = """You are a senior e-commerce SEO copywriter REVIEWING existing SEO assets \
for a product. Be honest: if the current value is already strong, say so and KEEP it. \
Do not rewrite for the sake of looking busy. Only suggest changes that measurably \
improve SEO (keyword coverage, length, clarity, brand alignment) or page quality score.

For each field, decide:
  verdict = "keep"     if the current value is already strong, no meaningful change needed
  verdict = "improve"  only if the new value is materially better

For BOTH verdicts, write a short rationale (≤140 chars, ONE sentence) explaining your call.

Output JSON ONLY, no commentary, exactly this structure:

{
  "seo_title": {
    "verdict": "keep" | "improve",
    "rationale": "short reason",
    "value": "string ≤60 chars (current if keep, new if improve)"
  },
  "meta_description": {
    "verdict": "keep" | "improve",
    "rationale": "short reason",
    "value": "string 120-155 chars"
  },
  "product_description": {
    "verdict": "keep" | "improve",
    "rationale": "short reason",
    "value": "valid HTML: 2-4 short paragraphs and 1 <ul> with 4-6 <li> features, 600-1200 chars"
  },
  "bullets": {
    "verdict": "improve",
    "rationale": "short reason",
    "value": ["5 short, scannable feature/benefit bullets", "...", "...", "...", "..."]
  }
}

Hard rules:
- seo_title ≤ 60 chars
- meta_description 120-155 chars
- Do not invent claims, ingredients, or certifications not in the brief
- Use the brand's voice if training is provided
- Natural keyword use, no stuffing
- Bullets verdict is always "improve" because we treat AI bullets as additive
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

    # Keywords the user pushed to specific SEO fields — must include
    targeted = db.execute(
        select(ProductKeyword.keyword, ProductKeyword.seo_targets)
        .where(ProductKeyword.product_id == product_id)
        .where(ProductKeyword.seo_targets.is_not(None))
    ).all()
    must_include_lines: list[str] = []
    for kw, targets_json in targeted:
        try:
            targets = json.loads(targets_json) if targets_json else []
        except (ValueError, TypeError):
            targets = []
        if targets:
            must_include_lines.append(
                f"- \"{kw}\" → must appear in: {', '.join(targets)}"
            )

    # User chat context (last 20 messages, oldest first) so Claude has the story
    chat_rows = db.execute(
        select(ProductChatMessage)
        .where(ProductChatMessage.product_id == product_id)
        .where(ProductChatMessage.topic == "seo")
        .order_by(ProductChatMessage.created_at.desc())
        .limit(20)
    ).scalars().all()
    chat_rows = list(reversed(chat_rows))
    chat_lines = "\n".join(
        f"  [{m.role}] {m.content[:500]}" for m in chat_rows
    ) or "  (no prior chat)"

    user_msg_parts = [
        _product_brief(db, product),
        f"Target keywords (use 2-4 naturally): {', '.join(keywords[:12])}",
    ]
    if must_include_lines:
        user_msg_parts.append(
            "Mandatory placements (these MUST be naturally included where listed):\n"
            + "\n".join(must_include_lines)
        )
    user_msg_parts.append(f"Recent chat context from the user:\n{chat_lines}")
    user_msg = "\n\n".join(user_msg_parts)
    text, err = claude_svc.chat(
        db, system=SEO_SYSTEM, user_message=user_msg, max_tokens=4000
    )
    if err or not text:
        return False, err or "Claude returned no text."
    data = _extract_json(text)
    if not data:
        return False, "Claude response was not parseable JSON."

    # Each entry in `data` is { verdict, rationale, value }
    field_map = [
        ("seo_title", "seo_title"),
        ("meta_description", "meta_description"),
        ("description", "product_description"),
        ("bullets", "bullets"),
    ]
    saved = 0
    for our_field, claude_field in field_map:
        block = data.get(claude_field)
        if not isinstance(block, dict):
            continue
        verdict = (block.get("verdict") or "improve").lower().strip()
        if verdict not in ("keep", "improve"):
            verdict = "improve"
        rationale = (block.get("rationale") or "").strip()[:300]
        raw_value = block.get("value")
        if raw_value is None:
            continue
        if our_field == "bullets":
            if not isinstance(raw_value, list):
                continue
            value = json.dumps([str(x)[:200] for x in raw_value][:10])
        else:
            value = str(raw_value)
        _replace_pending(db, product_id, our_field)
        db.add(
            ProductSeoDraft(
                product_id=product_id,
                field=our_field,
                suggested_value=value,
                status="pending",
                verdict=verdict,
                rationale=rationale or None,
            )
        )
        saved += 1
    db.commit()
    if saved == 0:
        return False, "Claude returned no usable suggestions."
    return True, f"Reviewed {saved} field(s) — see verdict and rationale below."


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
    db: Session,
    draft_id: int,
    user_id: int | None,
    edited_value: str | None = None,
) -> tuple[bool, str, ProductSeoDraft | None]:
    draft = db.get(ProductSeoDraft, draft_id)
    if draft is None or draft.status != "pending":
        return False, "Draft not found or already actioned.", None
    if edited_value is not None and edited_value.strip() and edited_value.strip() != draft.suggested_value:
        draft.suggested_value = edited_value.strip()
    draft.status = "approved"
    draft.approved_at = datetime.now(timezone.utc)
    draft.approved_by_user_id = user_id
    db.commit()
    return True, f"Approved {draft.field}.", draft


def reject_draft(db: Session, draft_id: int) -> tuple[bool, str]:
    draft = db.get(ProductSeoDraft, draft_id)
    if draft is None or draft.status != "pending":
        return False, "Draft not found or already actioned."
    draft.status = "rejected"
    db.commit()
    return True, f"Rejected {draft.field}."


def push_image_alt(
    db: Session, product_id: int, draft: ProductSeoDraft
) -> tuple[bool, str]:
    """Push an approved image-alt draft to Shopify and mirror the value locally."""
    from gglads.models.shopify_product import ShopifyProductImage

    if draft.field != "image_alt" or draft.image_id is None:
        return False, "Draft is not an image alt."
    if draft.status not in ("approved", "pending"):
        return False, "Draft is not approved."
    ok, msg = shopify_push_svc.update_image_alt(
        db, product_id, draft.image_id, draft.suggested_value
    )
    if not ok:
        return False, msg
    draft.pushed_to_shopify_at = datetime.now(timezone.utc)
    img = db.scalar(
        select(ShopifyProductImage)
        .where(ShopifyProductImage.id == draft.image_id)
        .where(ShopifyProductImage.product_id == product_id)
    )
    if img is not None:
        img.alt_text = draft.suggested_value
    db.commit()
    return True, "Pushed to Shopify."


def approve_and_push_image(
    db: Session,
    product_id: int,
    draft_id: int,
    user_id: int | None,
    edited_value: str | None = None,
) -> tuple[bool, str]:
    ok, detail, draft = approve_draft(db, draft_id, user_id, edited_value)
    if not ok or draft is None:
        return False, detail
    ok2, msg = push_image_alt(db, product_id, draft)
    if not ok2:
        return False, f"Approved locally but Shopify push failed: {msg}"
    return True, "Approved and pushed to Shopify."


def push_all_approved_image_alts(
    db: Session, product_id: int
) -> tuple[bool, str]:
    drafts = db.execute(
        select(ProductSeoDraft)
        .where(ProductSeoDraft.product_id == product_id)
        .where(ProductSeoDraft.field == "image_alt")
        .where(ProductSeoDraft.status == "approved")
        .where(ProductSeoDraft.pushed_to_shopify_at.is_(None))
    ).scalars().all()
    if not drafts:
        return False, "No approved image-alt drafts waiting to be pushed."
    pushed = 0
    failures: list[str] = []
    for d in drafts:
        ok, msg = push_image_alt(db, product_id, d)
        if ok:
            pushed += 1
        else:
            failures.append(f"#{d.image_id}: {msg}")
    if failures:
        return pushed > 0, f"Pushed {pushed}/{len(drafts)}. Errors: {'; '.join(failures)}"
    return True, f"Pushed {pushed} image alt(s) to Shopify."


def push_approved_seo_to_shopify(
    db: Session, product_id: int
) -> tuple[bool, str]:
    """Push every approved-not-yet-pushed SEO draft (title / meta / description)
    to Shopify in one productUpdate call, then mark them as pushed."""
    drafts = db.execute(
        select(ProductSeoDraft)
        .where(ProductSeoDraft.product_id == product_id)
        .where(ProductSeoDraft.field.in_(("seo_title", "meta_description", "description")))
        .where(ProductSeoDraft.status == "approved")
        .where(ProductSeoDraft.pushed_to_shopify_at.is_(None))
    ).scalars().all()
    if not drafts:
        return False, "No approved SEO drafts waiting to be pushed."

    by_field = {d.field: d for d in drafts}
    seo_title = by_field["seo_title"].suggested_value if "seo_title" in by_field else None
    meta_desc = (
        by_field["meta_description"].suggested_value if "meta_description" in by_field else None
    )
    description_html = (
        by_field["description"].suggested_value if "description" in by_field else None
    )

    ok, msg = shopify_push_svc.update_product_seo(
        db,
        product_id,
        description_html=description_html,
        seo_title=seo_title,
        seo_description=meta_desc,
    )
    if not ok:
        return False, msg

    # Mark all pushed and mirror values locally so the UI shows them as current.
    product = db.get(ShopifyProduct, product_id)
    now = datetime.now(timezone.utc)
    for d in drafts:
        d.pushed_to_shopify_at = now
        if product is not None:
            if d.field == "seo_title":
                product.seo_title = d.suggested_value
            elif d.field == "meta_description":
                product.seo_meta_description = d.suggested_value
            elif d.field == "description":
                product.description_html = d.suggested_value
    db.commit()
    pushed_names = ", ".join(sorted(by_field.keys()))
    return True, f"Pushed to Shopify: {pushed_names}."


def approve_and_push_all_pending_image_alts(
    db: Session, product_id: int, user_id: int | None
) -> tuple[bool, str]:
    pending = db.execute(
        select(ProductSeoDraft)
        .where(ProductSeoDraft.product_id == product_id)
        .where(ProductSeoDraft.field == "image_alt")
        .where(ProductSeoDraft.status == "pending")
    ).scalars().all()
    if not pending:
        return False, "No pending image-alt suggestions to approve."
    approved_pushed = 0
    failures: list[str] = []
    for d in pending:
        ok, _detail, draft = approve_draft(db, d.id, user_id)
        if not ok or draft is None:
            continue
        ok2, msg = push_image_alt(db, product_id, draft)
        if ok2:
            approved_pushed += 1
        else:
            failures.append(f"#{d.image_id}: {msg}")
    if failures:
        return (
            approved_pushed > 0,
            f"Approved+pushed {approved_pushed}/{len(pending)}. Errors: {'; '.join(failures)}",
        )
    return True, f"Approved and pushed {approved_pushed} image alt(s) to Shopify."
