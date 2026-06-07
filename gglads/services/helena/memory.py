"""Persistent learning memory.

Durable facts / preferences / decisions Helena has learned. Items are injected
into the agent's system prompt for every chat and scheduled task, so the user
never has to re-explain. The `remember` skill writes here automatically from
conversation; the Workspace/Memory page lets the user view, edit, and remove
items.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from gglads.models.helena import MemoryItem

VALID_CATEGORIES = ("preference", "fact", "decision", "general")


def _now() -> datetime:
    return datetime.now(UTC)


def list_items(db: Session, *, include_inactive: bool = True) -> list[MemoryItem]:
    q = select(MemoryItem).order_by(MemoryItem.created_at.desc())
    if not include_inactive:
        q = q.where(MemoryItem.is_active.is_(True))
    return list(db.scalars(q).all())


def add_item(
    db: Session, *, content: str, category: str = "general",
    source: str = "chat", user_id: int | None = None,
) -> MemoryItem | None:
    content = (content or "").strip()
    if not content:
        return None
    category = category if category in VALID_CATEGORIES else "general"
    # De-dupe: skip if an identical active memory already exists.
    existing = db.scalar(
        select(MemoryItem).where(MemoryItem.content == content, MemoryItem.is_active.is_(True))
    )
    if existing is not None:
        return existing
    item = MemoryItem(content=content, category=category, source=source,
                      is_active=True, created_by_user_id=user_id)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def update_item(
    db: Session, item_id: int, *, content: str | None = None,
    category: str | None = None, is_active: bool | None = None,
) -> MemoryItem | None:
    item = db.get(MemoryItem, item_id)
    if item is None:
        return None
    if content is not None and content.strip():
        item.content = content.strip()
    if category is not None and category in VALID_CATEGORIES:
        item.category = category
    if is_active is not None:
        item.is_active = is_active
    item.updated_at = _now()
    db.commit()
    db.refresh(item)
    return item


def delete_item(db: Session, item_id: int) -> None:
    item = db.get(MemoryItem, item_id)
    if item is not None:
        db.delete(item)
        db.commit()


def memory_context_text(db: Session, limit: int = 50) -> str:
    """Prompt-ready block of active memories for injection into the agent."""
    items = db.scalars(
        select(MemoryItem)
        .where(MemoryItem.is_active.is_(True))
        .order_by(MemoryItem.created_at.desc())
        .limit(limit)
    ).all()
    if not items:
        return ""
    lines = [f"- ({i.category}) {i.content}" for i in items]
    return "Things you have learned and must apply without being re-told:\n" + "\n".join(lines)
