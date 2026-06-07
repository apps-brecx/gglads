"""Workspace files — browse / download / delete the artifacts the agent makes.

Aggregates BrandAsset (generated images + videos, saved product images) and
EmailAsset (email hero/section images). Each file has a stable `ref`
("brandasset:<id>" / "emailasset:<id>") used for deletion. Deleting removes the
DB row and best-effort deletes the stored object.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from gglads.models.brand import BrandAsset
from gglads.models.email_campaign import EmailAsset
from gglads.services.helena import storage

_VIDEO_EXTS = (".mp4", ".mov", ".webm")


def _media_kind(url: str) -> str:
    u = (url or "").lower()
    return "video" if any(u.endswith(e) or e + "?" in u for e in _VIDEO_EXTS) else "image"


def list_files(db: Session) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for a in db.scalars(select(BrandAsset).order_by(BrandAsset.created_at.desc())).all():
        files.append({
            "ref": f"brandasset:{a.id}",
            "title": a.title or (a.prompt or "Generated asset")[:60] or "Asset",
            "url": a.url,
            "media": _media_kind(a.url),
            "source": a.kind,
            "created_at": a.created_at,
        })
    for e in db.scalars(select(EmailAsset).order_by(EmailAsset.created_at.desc())).all():
        files.append({
            "ref": f"emailasset:{e.id}",
            "title": e.alt_text or f"Email {e.role}",
            "url": e.url,
            "media": _media_kind(e.url),
            "source": f"email/{e.role}",
            "created_at": e.created_at,
        })
    files.sort(key=lambda f: f["created_at"] or "", reverse=True)
    return files


def delete_file(db: Session, ref: str) -> tuple[bool, str | None]:
    try:
        kind, sid = ref.split(":", 1)
        rid = int(sid)
    except (ValueError, AttributeError):
        return False, "Bad file reference."
    model = {"brandasset": BrandAsset, "emailasset": EmailAsset}.get(kind)
    if model is None:
        return False, "Unknown file type."
    row = db.get(model, rid)
    if row is None:
        return False, "File not found."
    url = row.url
    db.delete(row)
    db.commit()
    storage.delete_url(url)  # best-effort; missing/unconfigured is a no-op
    return True, None
