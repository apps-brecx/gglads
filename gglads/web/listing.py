"""Shared utilities for paginated / sortable / filterable list views."""

from __future__ import annotations

from typing import Any, Iterable
from urllib.parse import urlencode

from fastapi import Request

PER_PAGE_OPTIONS = (50, 100, 200, 500)


def parse_per_page(value: str | None, default: int = 50) -> int:
    if not value:
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n in PER_PAGE_OPTIONS else default


def parse_page(value: str | None) -> int:
    if not value:
        return 1
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 1
    return max(1, n)


def parse_sort(
    value: str | None, allowed: Iterable[str], default: str
) -> str:
    if value and value in set(allowed):
        return value
    return default


def parse_direction(value: str | None, default: str = "asc") -> str:
    return value if value in ("asc", "desc") else default


def paginate(items: list[Any], page: int, per_page: int) -> tuple[list[Any], int, int, int]:
    """Return (page_items, page, total_pages, total_items)."""
    total = len(items)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    start = (page - 1) * per_page
    return items[start : start + per_page], page, total_pages, total


def query_string_for(request: Request, *, drop: Iterable[str] = ()) -> str:
    """Re-encode the current query string excluding certain keys.

    Useful for building sort/pagination links that preserve other filters.
    """
    drop_set = set(drop)
    pairs = [(k, v) for k, v in request.query_params.multi_items() if k not in drop_set]
    return urlencode(pairs)


def sort_indicator(current_sort: str, current_dir: str, target_sort: str) -> str:
    if current_sort != target_sort:
        return ""
    return "↑" if current_dir == "asc" else "↓"


def next_sort_dir(current_sort: str, current_dir: str, target_sort: str) -> str:
    """If clicking the same header, flip direction; otherwise ascending."""
    if current_sort == target_sort:
        return "desc" if current_dir == "asc" else "asc"
    return "asc"
