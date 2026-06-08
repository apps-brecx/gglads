"""Resolve human date-range requests (incl. 'yesterday') to since/until dates."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta


def _today() -> date:
    return datetime.now(UTC).date()


def resolve_range(
    preset: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> tuple[date, date]:
    """Return (start_date, end_date) inclusive. Explicit since/until win;
    otherwise a preset is used; default last 7 days."""
    if since:
        try:
            s = date.fromisoformat(since[:10])
            e = date.fromisoformat(until[:10]) if until else _today()
            return s, e
        except ValueError:
            pass
    today = _today()
    p = (preset or "last_7d").strip().lower().replace("-", "_").replace(" ", "_")
    if p in ("today",):
        return today, today
    if p in ("yesterday",):
        y = today - timedelta(days=1)
        return y, y
    if p in ("last_7d", "7d", "last_week", "week"):
        return today - timedelta(days=6), today
    if p in ("last_14d", "14d"):
        return today - timedelta(days=13), today
    if p in ("last_30d", "30d", "last_month_rolling", "month"):
        return today - timedelta(days=29), today
    if p in ("this_month", "month_to_date", "mtd"):
        return today.replace(day=1), today
    if p in ("last_month",):
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        return last_prev.replace(day=1), last_prev
    return today - timedelta(days=6), today
