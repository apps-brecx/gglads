"""Worker task tracking — assign, mark done, audit.

The same task slugs (meta_title, meta_description, description, image_alts,
keywords, ad_campaign) work for products and collections; the entity_type
column disambiguates. Each (entity, slug) pair has at most ONE row in
entity_tasks, reused as the task moves through its lifecycle.

Reports and 'what's open' queries roll up from this table.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import and_, distinct, func, or_, select
from sqlalchemy.orm import Session, aliased

from gglads.models.entity_task import EntityTask
from gglads.models.shopify_product import ShopifyCollection, ShopifyProduct
from gglads.models.user import User

logger = logging.getLogger("gglads.tasks")


# ---------------------------------------------------------------------------
# Task catalog
# ---------------------------------------------------------------------------

# Allowed task slugs per entity type, with display labels. Adding a new slug
# only needs an edit here — no migration.
PRODUCT_TASK_TYPES: list[tuple[str, str]] = [
    ("meta_title", "Meta title"),
    ("meta_description", "Meta description"),
    ("description", "Description (body)"),
    ("image_alts", "Image alt text"),
    ("keywords", "Keywords researched + bucketed"),
    ("ad_campaign", "Ads campaign live"),
]

COLLECTION_TASK_TYPES: list[tuple[str, str]] = [
    ("meta_title", "Meta title"),
    ("meta_description", "Meta description"),
    ("description", "Description (body)"),
    ("ad_campaign", "Ads campaign live"),
]


def task_types_for(entity_type: str) -> list[tuple[str, str]]:
    if entity_type == "product":
        return PRODUCT_TASK_TYPES
    if entity_type == "collection":
        return COLLECTION_TASK_TYPES
    return []


def task_label(entity_type: str, slug: str) -> str:
    for s, label in task_types_for(entity_type):
        if s == slug:
            return label
    return slug.replace("_", " ").title()


def _valid_slug(entity_type: str, slug: str) -> bool:
    return slug in {s for s, _ in task_types_for(entity_type)}


def _entity_exists(db: Session, entity_type: str, entity_id: int) -> bool:
    if entity_type == "product":
        return db.scalar(
            select(func.count(ShopifyProduct.id)).where(ShopifyProduct.id == entity_id)
        ) or 0
    if entity_type == "collection":
        return db.scalar(
            select(func.count(ShopifyCollection.id)).where(
                ShopifyCollection.id == entity_id
            )
        ) or 0
    return False


# ---------------------------------------------------------------------------
# Core CRUD — used by the checkboxes on product/collection pages
# ---------------------------------------------------------------------------

def _get_or_create(
    db: Session,
    entity_type: str,
    entity_id: int,
    task_slug: str,
) -> EntityTask:
    row = db.scalar(
        select(EntityTask)
        .where(EntityTask.entity_type == entity_type)
        .where(EntityTask.entity_id == entity_id)
        .where(EntityTask.task_slug == task_slug)
    )
    if row is None:
        row = EntityTask(
            entity_type=entity_type, entity_id=entity_id, task_slug=task_slug
        )
        db.add(row)
        db.flush()
    return row


def mark_done(
    db: Session,
    entity_type: str,
    entity_id: int,
    task_slug: str,
    user_id: int,
    notes: str | None = None,
) -> tuple[bool, str]:
    if not _valid_slug(entity_type, task_slug):
        return False, f"Unknown task: {task_slug}"
    if not _entity_exists(db, entity_type, entity_id):
        return False, "Entity not found."
    row = _get_or_create(db, entity_type, entity_id, task_slug)
    now = datetime.now(timezone.utc)
    row.completed_by_user_id = user_id
    row.completed_at = now
    if notes is not None:
        row.notes = notes[:2000] or None
    row.updated_at = now
    db.commit()
    return True, f"Marked '{task_label(entity_type, task_slug)}' done."


def mark_undone(
    db: Session,
    entity_type: str,
    entity_id: int,
    task_slug: str,
) -> tuple[bool, str]:
    row = db.scalar(
        select(EntityTask)
        .where(EntityTask.entity_type == entity_type)
        .where(EntityTask.entity_id == entity_id)
        .where(EntityTask.task_slug == task_slug)
    )
    if row is None:
        return False, "Task wasn't marked done."
    row.completed_by_user_id = None
    row.completed_at = None
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    return True, f"Re-opened '{task_label(entity_type, task_slug)}'."


def assign(
    db: Session,
    entity_type: str,
    entity_id: int,
    task_slug: str,
    assignee_user_id: int,
    assigned_by_user_id: int,
) -> tuple[bool, str]:
    if not _valid_slug(entity_type, task_slug):
        return False, f"Unknown task: {task_slug}"
    if not _entity_exists(db, entity_type, entity_id):
        return False, "Entity not found."
    row = _get_or_create(db, entity_type, entity_id, task_slug)
    now = datetime.now(timezone.utc)
    row.assigned_to_user_id = assignee_user_id
    row.assigned_by_user_id = assigned_by_user_id
    row.assigned_at = now
    row.updated_at = now
    db.commit()
    return True, "Assigned."


def unassign(
    db: Session,
    entity_type: str,
    entity_id: int,
    task_slug: str,
) -> tuple[bool, str]:
    row = db.scalar(
        select(EntityTask)
        .where(EntityTask.entity_type == entity_type)
        .where(EntityTask.entity_id == entity_id)
        .where(EntityTask.task_slug == task_slug)
    )
    if row is None or row.assigned_to_user_id is None:
        return False, "Task wasn't assigned."
    row.assigned_to_user_id = None
    row.assigned_by_user_id = None
    row.assigned_at = None
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    return True, "Un-assigned."


def bulk_assign(
    db: Session,
    entity_type: str,
    entity_ids: list[int],
    task_slugs: list[str],
    assignee_user_id: int,
    assigned_by_user_id: int,
) -> tuple[bool, str, int]:
    """Assign one or more task slugs across many entities to a single worker."""
    if not entity_ids or not task_slugs:
        return False, "Pick at least one entity and task.", 0
    valid_slugs = [s for s in task_slugs if _valid_slug(entity_type, s)]
    if not valid_slugs:
        return False, "No valid task types.", 0
    n = 0
    for eid in entity_ids:
        if not _entity_exists(db, entity_type, eid):
            continue
        for slug in valid_slugs:
            row = _get_or_create(db, entity_type, eid, slug)
            row.assigned_to_user_id = assignee_user_id
            row.assigned_by_user_id = assigned_by_user_id
            row.assigned_at = datetime.now(timezone.utc)
            row.updated_at = row.assigned_at
            n += 1
    db.commit()
    return True, f"Assigned {n} task(s).", n


# ---------------------------------------------------------------------------
# Reads — what each page needs
# ---------------------------------------------------------------------------

def tasks_for_entity(
    db: Session, entity_type: str, entity_id: int
) -> dict[str, dict]:
    """Return {task_slug: row_dict} for every defined task on the entity.
    Slugs that haven't been touched come back as 'open + unassigned'."""
    rows = db.execute(
        select(EntityTask)
        .where(EntityTask.entity_type == entity_type)
        .where(EntityTask.entity_id == entity_id)
    ).scalars().all()
    by_slug: dict[str, EntityTask] = {r.task_slug: r for r in rows}
    # Resolve user names in one shot
    uids: set[int] = set()
    for r in rows:
        if r.assigned_to_user_id:
            uids.add(r.assigned_to_user_id)
        if r.completed_by_user_id:
            uids.add(r.completed_by_user_id)
    names_by_id: dict[int, str] = {}
    if uids:
        for u in db.execute(
            select(User).where(User.id.in_(uids))
        ).scalars().all():
            names_by_id[u.id] = u.name or u.email
    out: dict[str, dict] = {}
    for slug, label in task_types_for(entity_type):
        r = by_slug.get(slug)
        if r is None:
            out[slug] = {
                "slug": slug,
                "label": label,
                "assigned_to": None,
                "assigned_to_name": None,
                "assigned_at": None,
                "completed_by": None,
                "completed_by_name": None,
                "completed_at": None,
                "notes": None,
                "is_done": False,
                "is_assigned": False,
            }
        else:
            out[slug] = {
                "slug": slug,
                "label": label,
                "assigned_to": r.assigned_to_user_id,
                "assigned_to_name": names_by_id.get(r.assigned_to_user_id) if r.assigned_to_user_id else None,
                "assigned_at": r.assigned_at,
                "completed_by": r.completed_by_user_id,
                "completed_by_name": names_by_id.get(r.completed_by_user_id) if r.completed_by_user_id else None,
                "completed_at": r.completed_at,
                "notes": r.notes,
                "is_done": r.completed_at is not None,
                "is_assigned": r.assigned_to_user_id is not None,
            }
    return out


def progress_summary(
    db: Session, entity_type: str, entity_id: int
) -> dict:
    """Quick counts for the entity (used in headers / cards)."""
    expected = len(task_types_for(entity_type))
    done = db.scalar(
        select(func.count(EntityTask.id))
        .where(EntityTask.entity_type == entity_type)
        .where(EntityTask.entity_id == entity_id)
        .where(EntityTask.completed_at.is_not(None))
    ) or 0
    return {
        "expected": expected,
        "done": int(done),
        "open": expected - int(done),
        "pct": (100 * int(done) // expected) if expected else 0,
    }


def per_user_completed(
    db: Session,
    *,
    user_id: int | None = None,
    entity_type: str | None = None,
    task_slug: str | None = None,
    since: datetime | None = None,
    limit: int = 200,
) -> list[dict]:
    """Activity feed of completions, optionally filtered."""
    Completer = aliased(User)
    stmt = (
        select(EntityTask, Completer)
        .join(Completer, Completer.id == EntityTask.completed_by_user_id)
        .where(EntityTask.completed_at.is_not(None))
        .order_by(EntityTask.completed_at.desc())
        .limit(limit)
    )
    if user_id is not None:
        stmt = stmt.where(EntityTask.completed_by_user_id == user_id)
    if entity_type:
        stmt = stmt.where(EntityTask.entity_type == entity_type)
    if task_slug:
        stmt = stmt.where(EntityTask.task_slug == task_slug)
    if since is not None:
        stmt = stmt.where(EntityTask.completed_at >= since)

    rows = db.execute(stmt).all()
    if not rows:
        return []

    # Resolve entity titles in bulk.
    product_ids = [r.EntityTask.entity_id for r in rows if r.EntityTask.entity_type == "product"]
    collection_ids = [r.EntityTask.entity_id for r in rows if r.EntityTask.entity_type == "collection"]
    product_titles: dict[int, tuple[str, str]] = {}
    collection_titles: dict[int, tuple[str, str]] = {}
    if product_ids:
        for p in db.execute(
            select(ShopifyProduct.id, ShopifyProduct.title, ShopifyProduct.handle)
            .where(ShopifyProduct.id.in_(set(product_ids)))
        ).all():
            product_titles[p.id] = (p.title, f"/products/{p.id}")
    if collection_ids:
        for c in db.execute(
            select(ShopifyCollection.id, ShopifyCollection.title, ShopifyCollection.handle)
            .where(ShopifyCollection.id.in_(set(collection_ids)))
        ).all():
            collection_titles[c.id] = (c.title, f"/collections/{c.handle}")

    out: list[dict] = []
    for row in rows:
        t = row.EntityTask
        u = row[1]
        title_url = (
            product_titles.get(t.entity_id)
            if t.entity_type == "product"
            else collection_titles.get(t.entity_id)
        ) or ("(deleted)", "#")
        out.append({
            "task_id": t.id,
            "entity_type": t.entity_type,
            "entity_id": t.entity_id,
            "entity_title": title_url[0],
            "entity_url": title_url[1],
            "task_slug": t.task_slug,
            "task_label": task_label(t.entity_type, t.task_slug),
            "completed_at": t.completed_at,
            "completed_by": u.id,
            "completed_by_name": u.name or u.email,
        })
    return out


def per_user_summary(
    db: Session, since: datetime | None = None
) -> list[dict]:
    """User-level rollup of completion counts. Used by the admin report header."""
    stmt = (
        select(
            User.id, User.email, User.name,
            func.count(EntityTask.id).label("done_count"),
            func.max(EntityTask.completed_at).label("last_done_at"),
        )
        .join(EntityTask, EntityTask.completed_by_user_id == User.id)
        .where(EntityTask.completed_at.is_not(None))
        .group_by(User.id, User.email, User.name)
        .order_by(func.count(EntityTask.id).desc())
    )
    if since is not None:
        stmt = stmt.where(EntityTask.completed_at >= since)
    out: list[dict] = []
    for r in db.execute(stmt).all():
        out.append({
            "user_id": r.id,
            "user_label": r.name or r.email,
            "email": r.email,
            "done_count": int(r.done_count or 0),
            "last_done_at": r.last_done_at,
        })
    return out


def open_tasks_summary(db: Session) -> dict:
    """How many products/collections have at least one open task — overall and
    per task slug. 'Open' = no row OR row where completed_at IS NULL.

    Implemented as: (total entities × task types) − (completed task rows).
    """
    product_total = db.scalar(
        select(func.count(ShopifyProduct.id))
        .where(ShopifyProduct.is_ignored.is_(False))
        .where(ShopifyProduct.status != "draft")
    ) or 0
    collection_total = db.scalar(select(func.count(ShopifyCollection.id))) or 0
    product_done = db.scalar(
        select(func.count(EntityTask.id))
        .where(EntityTask.entity_type == "product")
        .where(EntityTask.completed_at.is_not(None))
    ) or 0
    collection_done = db.scalar(
        select(func.count(EntityTask.id))
        .where(EntityTask.entity_type == "collection")
        .where(EntityTask.completed_at.is_not(None))
    ) or 0
    product_expected = product_total * len(PRODUCT_TASK_TYPES)
    collection_expected = collection_total * len(COLLECTION_TASK_TYPES)
    return {
        "product": {
            "entities": int(product_total),
            "expected": int(product_expected),
            "done": int(product_done),
            "open": max(0, product_expected - int(product_done)),
        },
        "collection": {
            "entities": int(collection_total),
            "expected": int(collection_expected),
            "done": int(collection_done),
            "open": max(0, collection_expected - int(collection_done)),
        },
    }


def per_slug_open_counts(
    db: Session, entity_type: str
) -> dict[str, int]:
    """For each task slug under an entity_type, how many entities still
    have that task open. Used by 'filter by what's not done'."""
    if entity_type == "product":
        total = db.scalar(
            select(func.count(ShopifyProduct.id))
            .where(ShopifyProduct.is_ignored.is_(False))
            .where(ShopifyProduct.status != "draft")
        ) or 0
    else:
        total = db.scalar(select(func.count(ShopifyCollection.id))) or 0
    done_by_slug: dict[str, int] = {
        r.task_slug: int(r.n)
        for r in db.execute(
            select(EntityTask.task_slug, func.count(EntityTask.id).label("n"))
            .where(EntityTask.entity_type == entity_type)
            .where(EntityTask.completed_at.is_not(None))
            .group_by(EntityTask.task_slug)
        ).all()
    }
    out: dict[str, int] = {}
    for slug, _label in task_types_for(entity_type):
        out[slug] = max(0, int(total) - done_by_slug.get(slug, 0))
    return out


def entities_missing_task(
    db: Session,
    entity_type: str,
    task_slug: str,
    limit: int = 200,
    assigned_user_id: int | None = None,
    skip_assigned: bool = False,
) -> list[dict]:
    """List entities that don't yet have this task marked done. Useful for the
    'filter products by what's not done' view + a worker's 'my open work'."""
    if not _valid_slug(entity_type, task_slug):
        return []

    Done = aliased(EntityTask)
    Assigned = aliased(EntityTask)

    if entity_type == "product":
        base = (
            select(ShopifyProduct.id, ShopifyProduct.title, ShopifyProduct.handle)
            .where(ShopifyProduct.is_ignored.is_(False))
            .where(ShopifyProduct.status != "draft")
        )
        # left join to find the done row (if any)
        base = base.outerjoin(
            Done,
            and_(
                Done.entity_type == "product",
                Done.entity_id == ShopifyProduct.id,
                Done.task_slug == task_slug,
                Done.completed_at.is_not(None),
            ),
        ).where(Done.id.is_(None))
        if assigned_user_id is not None:
            base = base.join(
                Assigned,
                and_(
                    Assigned.entity_type == "product",
                    Assigned.entity_id == ShopifyProduct.id,
                    Assigned.task_slug == task_slug,
                    Assigned.assigned_to_user_id == assigned_user_id,
                ),
            )
        elif skip_assigned:
            base = base.outerjoin(
                Assigned,
                and_(
                    Assigned.entity_type == "product",
                    Assigned.entity_id == ShopifyProduct.id,
                    Assigned.task_slug == task_slug,
                    Assigned.assigned_to_user_id.is_not(None),
                ),
            ).where(Assigned.id.is_(None))
        base = base.order_by(ShopifyProduct.title).limit(limit)
        return [
            {"id": r.id, "title": r.title, "url": f"/products/{r.id}"}
            for r in db.execute(base).all()
        ]
    if entity_type == "collection":
        base = select(ShopifyCollection.id, ShopifyCollection.title, ShopifyCollection.handle)
        base = base.outerjoin(
            Done,
            and_(
                Done.entity_type == "collection",
                Done.entity_id == ShopifyCollection.id,
                Done.task_slug == task_slug,
                Done.completed_at.is_not(None),
            ),
        ).where(Done.id.is_(None))
        if assigned_user_id is not None:
            base = base.join(
                Assigned,
                and_(
                    Assigned.entity_type == "collection",
                    Assigned.entity_id == ShopifyCollection.id,
                    Assigned.task_slug == task_slug,
                    Assigned.assigned_to_user_id == assigned_user_id,
                ),
            )
        elif skip_assigned:
            base = base.outerjoin(
                Assigned,
                and_(
                    Assigned.entity_type == "collection",
                    Assigned.entity_id == ShopifyCollection.id,
                    Assigned.task_slug == task_slug,
                    Assigned.assigned_to_user_id.is_not(None),
                ),
            ).where(Assigned.id.is_(None))
        base = base.order_by(ShopifyCollection.title).limit(limit)
        return [
            {"id": r.id, "title": r.title, "url": f"/collections/{r.handle}"}
            for r in db.execute(base).all()
        ]
    return []


def my_assigned_open(db: Session, user_id: int) -> list[dict]:
    """Tasks assigned to this user that aren't done yet — used on /tasks/me."""
    rows = db.execute(
        select(EntityTask)
        .where(EntityTask.assigned_to_user_id == user_id)
        .where(EntityTask.completed_at.is_(None))
        .order_by(EntityTask.assigned_at.desc().nullslast())
    ).scalars().all()
    if not rows:
        return []
    product_ids = [r.entity_id for r in rows if r.entity_type == "product"]
    collection_ids = [r.entity_id for r in rows if r.entity_type == "collection"]
    p_titles: dict[int, tuple[str, str]] = {}
    c_titles: dict[int, tuple[str, str]] = {}
    if product_ids:
        for p in db.execute(
            select(ShopifyProduct.id, ShopifyProduct.title)
            .where(ShopifyProduct.id.in_(set(product_ids)))
        ).all():
            p_titles[p.id] = (p.title, f"/products/{p.id}")
    if collection_ids:
        for c in db.execute(
            select(ShopifyCollection.id, ShopifyCollection.title, ShopifyCollection.handle)
            .where(ShopifyCollection.id.in_(set(collection_ids)))
        ).all():
            c_titles[c.id] = (c.title, f"/collections/{c.handle}")
    out: list[dict] = []
    for r in rows:
        info = (
            p_titles.get(r.entity_id)
            if r.entity_type == "product"
            else c_titles.get(r.entity_id)
        ) or ("(deleted)", "#")
        out.append({
            "id": r.id,
            "entity_type": r.entity_type,
            "entity_id": r.entity_id,
            "entity_title": info[0],
            "entity_url": info[1],
            "task_slug": r.task_slug,
            "task_label": task_label(r.entity_type, r.task_slug),
            "assigned_at": r.assigned_at,
        })
    return out


def product_ids_unassigned(db: Session) -> list[int]:
    """Product ids that have NO entity_tasks row with an assignee set.
    'Unassigned' is interpreted at the product level — even if just one
    task has been assigned, the product counts as assigned."""
    rows = db.execute(
        select(EntityTask.entity_id)
        .where(EntityTask.entity_type == "product")
        .where(EntityTask.assigned_to_user_id.is_not(None))
        .distinct()
    ).scalars().all()
    if not rows:
        # Nobody has been assigned anything — every product is unassigned.
        all_ids = db.execute(select(ShopifyProduct.id)).scalars().all()
        return list(all_ids)
    assigned: set[int] = set(rows)
    all_ids = db.execute(select(ShopifyProduct.id)).scalars().all()
    return [pid for pid in all_ids if pid not in assigned]


def product_ids_missing_task(db: Session, task_slug: str) -> list[int]:
    """Product ids that don't have a completed entity_tasks row for this slug."""
    if not _valid_slug("product", task_slug):
        return []
    done_ids = db.execute(
        select(EntityTask.entity_id)
        .where(EntityTask.entity_type == "product")
        .where(EntityTask.task_slug == task_slug)
        .where(EntityTask.completed_at.is_not(None))
    ).scalars().all()
    done: set[int] = set(done_ids)
    all_ids = db.execute(select(ShopifyProduct.id)).scalars().all()
    return [pid for pid in all_ids if pid not in done]


def product_ids_has_open(db: Session) -> list[int]:
    """Product ids that have at least one task not yet done. (Any product
    that doesn't have ALL its task slugs completed.)"""
    expected = len(PRODUCT_TASK_TYPES)
    fully_done_rows = db.execute(
        select(EntityTask.entity_id, func.count(EntityTask.id).label("n"))
        .where(EntityTask.entity_type == "product")
        .where(EntityTask.completed_at.is_not(None))
        .group_by(EntityTask.entity_id)
        .having(func.count(EntityTask.id) >= expected)
    ).all()
    fully_done: set[int] = {r.entity_id for r in fully_done_rows}
    all_ids = db.execute(select(ShopifyProduct.id)).scalars().all()
    return [pid for pid in all_ids if pid not in fully_done]


def list_active_users(db: Session) -> list[dict]:
    """Workers available for assignment."""
    rows = db.execute(
        select(User)
        .where(User.is_active.is_(True))
        .order_by(User.name.nullslast(), User.email)
    ).scalars().all()
    return [{"id": u.id, "label": u.name or u.email, "email": u.email, "role": u.role} for u in rows]
