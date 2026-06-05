"""Content-calendar backend (CAL-DETAIL).

Builds Week and Month grids where every day cell carries the full vertical
stack of per-channel slots. Scheduled items render inline on their channel's
row; empty slots are selectable to open the add-content flow for that
channel + date.

Items come from helena_posts (one per channel via Post.channel) and
helena_email_campaigns (the 'email' channel). Status drives the chip color.
"""

from __future__ import annotations

from calendar import Calendar
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from gglads.models.email_campaign import EmailCampaign
from gglads.models.helena import Post

# Ordered channel rows shown in every day cell.
CHANNELS: list[dict[str, str]] = [
    {"key": "blog", "name": "Blog / Doc", "icon": "📝"},
    {"key": "linkedin", "name": "LinkedIn", "icon": "in"},
    {"key": "x", "name": "X", "icon": "𝕏"},
    {"key": "instagram", "name": "Instagram", "icon": "📷"},
    {"key": "pinterest", "name": "Pinterest", "icon": "📌"},
    {"key": "youtube", "name": "YouTube", "icon": "▶"},
    {"key": "tiktok", "name": "TikTok", "icon": "♪"},
    {"key": "facebook", "name": "Facebook", "icon": "f"},
    {"key": "email", "name": "Email", "icon": "✉"},
]
CHANNEL_KEYS = {c["key"] for c in CHANNELS}

# Post.status / EmailCampaign.status -> queue status bucket for coloring.
_STATUS_BUCKET = {
    "draft": "scheduled",
    "scheduled": "scheduled",
    "pending_approval": "scheduled",
    "publishing": "scheduled",
    "published": "published",
    "sent": "published",
    "failed": "failed",
}


def _now() -> datetime:
    return datetime.now(UTC)


def parse_ref(date_str: str | None) -> date:
    if date_str:
        try:
            return date.fromisoformat(date_str)
        except ValueError:
            pass
    return _now().date()


def _item_from_post(p: Post) -> dict[str, Any]:
    return {
        "id": p.id, "kind": "post", "channel": p.channel,
        "title": (p.caption or "Untitled")[:60],
        "status": _STATUS_BUCKET.get(p.status, "scheduled"),
        "time": p.scheduled_at.strftime("%H:%M") if p.scheduled_at else "",
        "url": p.permalink or "",
    }


def _item_from_email(e: EmailCampaign) -> dict[str, Any]:
    return {
        "id": e.id, "kind": "email", "channel": "email",
        "title": (e.subject or e.name or "Email")[:60],
        "status": _STATUS_BUCKET.get(e.status, "scheduled"),
        "time": e.scheduled_at.strftime("%H:%M") if e.scheduled_at else "",
        "url": f"/helena/email/{e.id}/preview",
    }


def _items_by_day_channel(db: Session, start: date, end: date) -> dict[str, dict[str, list]]:
    """Map ISO-date -> channel-key -> [items] within [start, end]."""
    grid: dict[str, dict[str, list]] = {}

    posts = db.scalars(
        select(Post).where(Post.scheduled_at.is_not(None))
    ).all()
    for p in posts:
        d = p.scheduled_at.date()
        if start <= d <= end:
            ch = p.channel if p.channel in CHANNEL_KEYS else "instagram"
            grid.setdefault(d.isoformat(), {}).setdefault(ch, []).append(_item_from_post(p))

    emails = db.scalars(
        select(EmailCampaign).where(EmailCampaign.scheduled_at.is_not(None))
    ).all()
    for e in emails:
        d = e.scheduled_at.date()
        if start <= d <= end:
            grid.setdefault(d.isoformat(), {}).setdefault("email", []).append(_item_from_email(e))

    return grid


def _day_cell(d: date, ref_month: int, today: date, items: dict[str, list]) -> dict[str, Any]:
    return {
        "date": d.isoformat(),
        "day": d.day,
        "in_month": d.month == ref_month,
        "is_today": d == today,
        "is_weekend": d.weekday() >= 5,
        "slots": [
            {"channel": c["key"], "icon": c["icon"], "name": c["name"],
             "items": items.get(c["key"], [])}
            for c in CHANNELS
        ],
    }


def view_data(db: Session, view: str, ref: date) -> dict[str, Any]:
    view = "week" if view == "week" else "month"
    today = _now().date()

    if view == "week":
        monday = ref - timedelta(days=ref.weekday())
        days = [monday + timedelta(days=i) for i in range(7)]
        weeks = [days]
        span_start, span_end = days[0], days[-1]
        prev_ref = (monday - timedelta(days=7)).isoformat()
        next_ref = (monday + timedelta(days=7)).isoformat()
        label = f"Week of {monday.strftime('%b %-d, %Y')}"
        ref_month = ref.month
    else:
        cal = Calendar(firstweekday=0)  # Monday
        month_weeks = cal.monthdatescalendar(ref.year, ref.month)
        weeks = month_weeks
        span_start = month_weeks[0][0]
        span_end = month_weeks[-1][-1]
        first = ref.replace(day=1)
        prev_ref = (first - timedelta(days=1)).replace(day=1).isoformat()
        nxt = (first + timedelta(days=32)).replace(day=1)
        next_ref = nxt.isoformat()
        label = ref.strftime("%B %Y")
        ref_month = ref.month

    items = _items_by_day_channel(db, span_start, span_end)
    grid_weeks = [
        [_day_cell(d, ref_month, today, items.get(d.isoformat(), {})) for d in week]
        for week in weeks
    ]

    return {
        "view": view,
        "weeks": grid_weeks,
        "channels": CHANNELS,
        "label": label,
        "today": today.isoformat(),
        "ref": ref.isoformat(),
        "prev_ref": prev_ref,
        "next_ref": next_ref,
        "weekday_names": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    }


def add_slot_item(
    db: Session,
    *,
    channel: str,
    day: date,
    caption: str = "",
    user_id: int | None = None,
) -> Post | EmailCampaign:
    """Create a draft item for a channel + date from the add-content flow."""
    when = datetime(day.year, day.month, day.day, 9, 0, tzinfo=UTC)
    if channel == "email":
        camp = EmailCampaign(
            name=caption or f"Email — {day.isoformat()}",
            subject=caption or None, status="draft",
            scheduled_at=when, created_by_user_id=user_id,
        )
        db.add(camp)
        db.commit()
        db.refresh(camp)
        return camp
    ch = channel if channel in CHANNEL_KEYS else "instagram"
    post = Post(
        caption=caption, channel=ch, status="draft",
        scheduled_at=when, created_by_user_id=user_id,
    )
    db.add(post)
    db.commit()
    db.refresh(post)
    return post
