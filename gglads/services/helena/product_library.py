"""Product image library.

High-quality product (bottle) images labeled with flavor + variant (Regular /
Sugar-Free), plus other reference files. Helena pulls the correct image when
generating content via find_image(); the library summary is injected into the
agent context so it knows what's available.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from gglads.models.helena import ProductImage
from gglads.services.helena import storage

logger = logging.getLogger("gglads.helena.product_library")

VARIANTS = ("regular", "sugar_free")
_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}


def _norm_variant(v: str | None) -> str | None:
    if not v:
        return None
    v = v.strip().lower().replace("-", "_").replace(" ", "_")
    if v in ("sugarfree", "sugar_free", "sf", "diet"):
        return "sugar_free"
    if v in ("regular", "reg", "full_sugar", "original"):
        return "regular"
    return None


def list_images(db: Session, kind: str | None = None) -> list[ProductImage]:
    q = select(ProductImage).order_by(ProductImage.created_at.desc())
    if kind:
        q = q.where(ProductImage.kind == kind)
    return list(db.scalars(q).all())


def add_image(
    db: Session, *, data: bytes, content_type: str = "image/png",
    flavor: str | None = None, variant: str | None = None,
    label: str | None = None, kind: str = "product",
    alt_text: str | None = None, user_id: int | None = None,
) -> tuple[ProductImage | None, str | None]:
    """Upload bytes to storage and record a library entry. Returns (row, error)."""
    ext = _EXT.get((content_type or "").lower(), "png")
    url, err = storage.put_bytes(data, content_type=content_type,
                                 key_prefix="helena/library", ext=ext)
    if err:
        return None, err
    variant = _norm_variant(variant) if kind == "product" else None
    flavor = (flavor or "").strip() or None
    if not label:
        if kind == "product" and flavor:
            label = f"{flavor}" + (f" ({variant.replace('_', '-')})" if variant else "")
        else:
            label = "Reference file"
    row = ProductImage(
        kind="reference" if kind != "product" else "product",
        flavor=flavor, variant=variant, label=label, url=url,
        alt_text=alt_text or label, content_type=content_type,
        created_by_user_id=user_id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row, None


def delete_image(db: Session, image_id: int) -> None:
    row = db.get(ProductImage, image_id)
    if row is not None:
        url = row.url
        db.delete(row)
        db.commit()
        storage.delete_url(url)  # best-effort


def find_image(db: Session, flavor: str, variant: str | None = None) -> ProductImage | None:
    """Best product-image match for a flavor (and optional variant)."""
    flavor = (flavor or "").strip().lower()
    if not flavor:
        return None
    candidates = db.scalars(
        select(ProductImage).where(ProductImage.kind == "product")
        .where(ProductImage.flavor.is_not(None))
        .order_by(ProductImage.created_at.desc())
    ).all()
    want_variant = _norm_variant(variant)
    matches = [c for c in candidates if c.flavor and flavor in c.flavor.lower()]
    if want_variant:
        exact = [c for c in matches if c.variant == want_variant]
        if exact:
            return exact[0]
    return matches[0] if matches else None


def library_context_text(db: Session, limit: int = 40) -> str:
    """Compact summary of available product images for the agent context."""
    rows = list_images(db, kind="product")[:limit]
    if not rows:
        return ""
    lines = []
    for r in rows:
        v = (r.variant or "").replace("_", "-") or "unspecified"
        lines.append(f"- {r.flavor or 'unlabeled'} ({v})")
    return ("Product image library (use find_product_image to fetch the exact URL "
            "before generating content):\n" + "\n".join(lines))
