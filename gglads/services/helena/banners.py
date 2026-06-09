"""Website banners.

Generate on-brand website banners at your exact pixel sizes. The bottle is the
real bottle (via the generate_image skill); the image model renders at the
nearest aspect ratio and we cover-crop to the precise width x height with
Pillow. Sizes and sample designs are configured in Banner settings; samples can
be remixed (recreated for another flavor/text) in chat.
"""

from __future__ import annotations

import io
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from gglads.models.helena import Banner, BannerSample, BannerSize

logger = logging.getLogger("gglads.helena.banners")


def _now() -> datetime:
    return datetime.now(UTC)


# --- Sizes (settings) ---------------------------------------------------

def list_sizes(db: Session) -> list[BannerSize]:
    return list(db.scalars(select(BannerSize).order_by(BannerSize.created_at)).all())


def add_size(db: Session, *, name: str, width: int, height: int,
             notes: str | None = None) -> BannerSize | None:
    name = (name or "").strip()
    if not name or not width or not height:
        return None
    row = BannerSize(name=name[:120], width=int(width), height=int(height), notes=notes)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def delete_size(db: Session, size_id: int) -> None:
    row = db.get(BannerSize, size_id)
    if row is not None:
        db.delete(row)
        db.commit()


# --- Samples ------------------------------------------------------------

def list_samples(db: Session) -> list[BannerSample]:
    return list(db.scalars(select(BannerSample).order_by(BannerSample.created_at.desc())).all())


def add_sample(db: Session, *, name: str, image_url: str,
               notes: str | None = None) -> BannerSample | None:
    image_url = (image_url or "").strip()
    if not image_url:
        return None
    row = BannerSample(name=(name or "Banner sample").strip()[:255],
                       image_url=image_url, notes=notes)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def delete_sample(db: Session, sample_id: int) -> None:
    row = db.get(BannerSample, sample_id)
    if row is not None:
        db.delete(row)
        db.commit()


# --- Banners ------------------------------------------------------------

def list_banners(db: Session, limit: int = 200) -> list[Banner]:
    return list(db.scalars(
        select(Banner).order_by(Banner.created_at.desc()).limit(limit)).all())


def get(db: Session, banner_id: int) -> Banner | None:
    return db.get(Banner, banner_id)


def create_banner(db: Session, *, name: str, width: int, height: int,
                  flavor: str | None = None, variant: str | None = None,
                  concept: str | None = None, user_id: int | None = None) -> Banner:
    b = Banner(name=(name or "Banner").strip()[:255], width=int(width), height=int(height),
               flavor=flavor or None, variant=variant or None, concept=concept or None,
               status="draft", created_by_user_id=user_id)
    db.add(b)
    db.commit()
    db.refresh(b)
    return b


def delete_banner(db: Session, banner_id: int) -> None:
    b = db.get(Banner, banner_id)
    if b is not None:
        db.delete(b)
        db.commit()


def _nearest_aspect(width: int, height: int) -> str:
    """Map exact pixels to the image model's supported aspect ratio."""
    if not height:
        return "1:1"
    r = width / height
    options = {"1:1": 1.0, "16:9": 16 / 9, "9:16": 9 / 16}
    return min(options, key=lambda k: abs(options[k] - r))


def generate(db: Session, banner: Banner, *, user_id: int | None = None) -> dict:
    """Generate the banner image (real bottle) and crop it to the exact size."""
    from gglads.services.helena import skills as skills_svc
    concept = (banner.concept or "").strip() or (
        f"On-brand website banner for our drink{(' — ' + banner.flavor) if banner.flavor else ''}. "
        "Clean, premium, with clear space for a headline and a call-to-action button.")
    args: dict[str, Any] = {"concept": concept, "aspect_ratio": _nearest_aspect(banner.width,
                                                                                banner.height)}
    if banner.flavor:
        args["flavor"] = banner.flavor
    if banner.variant:
        args["variant"] = banner.variant
    res = skills_svc.run_skill(db, "generate_image", args, user_id=user_id, session_id=None)
    if not res.get("ok") or not res.get("images"):
        return {"ok": False, "error": res.get("error", "Couldn't generate the image.")}
    src_url = res["images"][0]["url"]
    cropped, err = _crop_to_size(src_url, banner.width, banner.height)
    final_url = cropped or src_url  # fall back to the un-cropped image if Pillow fails
    if err:
        logger.info("banner crop fallback (%sx%s): %s", banner.width, banner.height, err)
    banner.image_url = final_url
    banner.status = "ready"
    banner.updated_at = _now()
    db.commit()
    return {"ok": True, "image_url": final_url, "exact": bool(cropped)}


def _crop_to_size(url: str, width: int, height: int) -> tuple[str | None, str | None]:
    """Download an image and cover-crop it to exactly width x height, then store
    it. Returns (url, error)."""
    import httpx

    from gglads.services.helena import storage
    try:
        from PIL import Image
    except ImportError as exc:
        return None, f"Pillow unavailable: {exc}"
    try:
        r = httpx.get(url, timeout=30.0, follow_redirects=True)
        if r.status_code != 200 or not r.content:
            return None, "couldn't fetch generated image"
        img = Image.open(io.BytesIO(r.content)).convert("RGB")
        sw, sh = img.size
        # cover-fit: scale so the image fills the box, then center-crop.
        scale = max(width / sw, height / sh)
        nw, nh = max(1, round(sw * scale)), max(1, round(sh * scale))
        img = img.resize((nw, nh))
        left, top = (nw - width) // 2, (nh - height) // 2
        img = img.crop((left, top, left + width, top + height))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        out_url, serr = storage.put_bytes(buf.getvalue(), content_type="image/png",
                                          key_prefix="helena/banner", ext="png")
        if serr:
            return None, serr
        return out_url, None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
