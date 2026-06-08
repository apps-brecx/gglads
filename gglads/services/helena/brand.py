"""Brand knowledge base + the ShopifyProductProvider adapter.

Brand: a single editable record (id=1 by convention) seeded from existing
global ProductChatMessage rows the first time it's needed. The brand context
string produced here is injected into image prompts and copy generation.

ShopifyProductProvider: a thin adapter over the already-synced Shopify tables
so Helena reads products/images without knowing the storage details — the
same indirection the brief asks for (listProducts / getProduct /
getProductImages).
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from gglads.models.brand import Brand, BrandAsset, BrandDocument
from gglads.models.product_chat import ProductChatMessage
from gglads.models.shopify_product import ShopifyProduct, ShopifyProductImage

# ---------------------------------------------------------------------------
# Brand
# ---------------------------------------------------------------------------

def get_or_create_brand(db: Session) -> Brand:
    brand = db.scalar(select(Brand).order_by(Brand.id).limit(1))
    if brand is not None:
        return brand
    # Seed from existing global brand-voice chat (product_id IS NULL).
    seeds = db.scalars(
        select(ProductChatMessage)
        .where(ProductChatMessage.product_id.is_(None))
        .where(ProductChatMessage.role == "user")
        .order_by(ProductChatMessage.created_at)
        .limit(20)
    ).all()
    voice = "\n".join(m.content for m in seeds) if seeds else None
    brand = Brand(name="Our brand", tone=voice)
    db.add(brand)
    db.commit()
    db.refresh(brand)
    return brand


def update_brand(db: Session, fields: dict[str, Any], user_id: int | None) -> Brand:
    brand = get_or_create_brand(db)
    for key in (
        "name", "tone", "visual_style", "mood", "audience",
        "content_themes", "guidelines",
    ):
        if key in fields:
            setattr(brand, key, (fields.get(key) or "").strip() or None)
    if "palette" in fields:
        colors = [c.strip() for c in str(fields["palette"]).replace(",", " ").split() if c.strip()]
        brand.palette_json = json.dumps(colors) if colors else None
    brand.updated_by_user_id = user_id
    db.commit()
    db.refresh(brand)
    return brand


def brand_context_text(db: Session) -> str:
    """Compact, prompt-ready brand brief injected into generation calls."""
    brand = get_or_create_brand(db)
    palette = ""
    if brand.palette_json:
        try:
            palette = ", ".join(json.loads(brand.palette_json))
        except json.JSONDecodeError:
            palette = brand.palette_json
    parts = [
        f"Brand: {brand.name}",
        f"Tone: {brand.tone}" if brand.tone else "",
        f"Visual style: {brand.visual_style}" if brand.visual_style else "",
        f"Mood: {brand.mood}" if brand.mood else "",
        f"Audience: {brand.audience}" if brand.audience else "",
        f"Content themes: {brand.content_themes}" if brand.content_themes else "",
        f"Palette: {palette}" if palette else "",
        f"Guidelines: {brand.guidelines}" if brand.guidelines else "",
    ]
    # Append brand-knowledge documents (persistent memory) as excerpts.
    docs = list_documents(db)
    if docs:
        parts.append("\nBrand knowledge documents:")
        for d in docs:
            excerpt = (d.content or "").strip().replace("\n", " ")
            if len(excerpt) > 600:
                excerpt = excerpt[:600] + "…"
            line = f"- {d.title}: {excerpt}" if excerpt else f"- {d.title}"
            if d.url:
                line += f" ({d.url})"
            parts.append(line)
    return "\n".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Brand knowledge documents (persistent memory)
# ---------------------------------------------------------------------------

def list_documents(db: Session) -> list[BrandDocument]:
    brand = get_or_create_brand(db)
    return list(
        db.scalars(
            select(BrandDocument)
            .where(BrandDocument.brand_id == brand.id)
            .order_by(BrandDocument.created_at.desc())
        ).all()
    )


def add_document(
    db: Session, *, title: str, content: str | None = None,
    url: str | None = None, user_id: int | None = None,
) -> BrandDocument:
    brand = get_or_create_brand(db)
    doc = BrandDocument(
        brand_id=brand.id, title=title.strip()[:255] or "Untitled",
        content=(content or "").strip() or None, url=(url or "").strip() or None,
        created_by_user_id=user_id,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


def delete_document(db: Session, doc_id: int) -> None:
    doc = db.get(BrandDocument, doc_id)
    if doc is not None:
        db.delete(doc)
        db.commit()


def list_assets(db: Session, kind: str | None = None) -> list[BrandAsset]:
    brand = get_or_create_brand(db)
    q = select(BrandAsset).where(BrandAsset.brand_id == brand.id)
    if kind:
        q = q.where(BrandAsset.kind == kind)
    return list(db.scalars(q.order_by(BrandAsset.created_at.desc())).all())


def save_asset(
    db: Session,
    *,
    url: str,
    kind: str = "generated",
    title: str | None = None,
    prompt: str | None = None,
    product_id: int | None = None,
    user_id: int | None = None,
) -> BrandAsset:
    brand = get_or_create_brand(db)
    asset = BrandAsset(
        brand_id=brand.id,
        kind=kind,
        title=title,
        url=url,
        prompt=prompt,
        product_id=product_id,
        created_by_user_id=user_id,
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return asset


# ---------------------------------------------------------------------------
# ShopifyProductProvider adapter
# ---------------------------------------------------------------------------

class ShopifyProductProvider:
    """Reads product data from the already-synced Shopify tables. The rest of
    Helena depends on this interface, not on the table layout."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def list_products(self, limit: int = 50, query: str | None = None) -> list[dict[str, Any]]:
        q = (
            select(ShopifyProduct)
            .where(ShopifyProduct.is_ignored.is_(False))
            .where(ShopifyProduct.status == "active")
            .order_by(ShopifyProduct.units_sold_90d.desc())
            .limit(limit)
        )
        if query:
            like = f"%{query}%"
            q = q.where(ShopifyProduct.title.ilike(like))
        return [self._to_dict(p) for p in self._db.scalars(q).all()]

    def get_product(self, product_id: int) -> dict[str, Any] | None:
        p = self._db.get(ShopifyProduct, product_id)
        return self._to_dict(p) if p else None

    def get_product_images(self, product_id: int) -> list[dict[str, Any]]:
        imgs = self._db.scalars(
            select(ShopifyProductImage)
            .where(ShopifyProductImage.product_id == product_id)
            .order_by(ShopifyProductImage.position)
        ).all()
        return [{"url": i.url, "alt": i.alt_text, "position": i.position} for i in imgs]

    def _to_dict(self, p: ShopifyProduct) -> dict[str, Any]:
        return {
            "id": p.id,
            "title": p.title,
            "handle": p.handle,
            "vendor": p.vendor,
            "product_type": p.product_type,
            "price_min": float(p.price_min) if p.price_min is not None else None,
            "price_max": float(p.price_max) if p.price_max is not None else None,
            "currency": p.currency,
            "image_url": p.image_url,
            "url": p.shopify_admin_url,
            "description_html": p.description_html,
        }

    def product_context_text(self, product_id: int) -> str:
        p = self.get_product(product_id)
        if not p:
            return ""
        price = f"${p['price_min']:.2f}" if p.get("price_min") is not None else "—"
        return (
            f"Product: {p['title']}\n"
            f"Type: {p.get('product_type') or '—'} | Vendor: {p.get('vendor') or '—'}\n"
            f"Price: {price}\n"
            f"Primary image: {p.get('image_url') or '(none)'}"
        )
