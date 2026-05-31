"""Per-user preferences stored as JSON on users.preferences."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from gglads.models.user import User

# Default Keyword-page columns when user has no preference set
KEYWORD_PAGE_DEFAULT_COLS: list[str] = [
    "source",
    "rationale",
    "volume",
    "competition",
    "org_pos",
    "org_clicks",
    "org_impr",
    "cov_title",
    "cov_meta_title",
    "cov_meta_description",
    "cov_description",
    "cov_image_alts",
    "ads",
    "score",
]

# Allowed sort keys on Keywords page (subset that's safe to default to)
KEYWORD_PAGE_SORT_KEYS = (
    "keyword",
    "source",
    "volume",
    "competition",
    "position",
    "clicks",
    "impressions",
    "ctr",
    "score",
    "bucket",
)


def load_prefs(user: User) -> dict[str, Any]:
    if not user or not user.preferences:
        return {}
    try:
        v = json.loads(user.preferences)
        return v if isinstance(v, dict) else {}
    except (ValueError, TypeError):
        return {}


def save_prefs(db: Session, user: User, prefs: dict[str, Any]) -> None:
    user.preferences = json.dumps(prefs)
    db.commit()


def keyword_page_defaults(user: User) -> dict[str, Any]:
    p = load_prefs(user).get("keyword_page") or {}
    cols_raw = p.get("cols")
    cols = cols_raw if isinstance(cols_raw, list) and cols_raw else KEYWORD_PAGE_DEFAULT_COLS
    sort = p.get("sort") if p.get("sort") in KEYWORD_PAGE_SORT_KEYS else "score"
    direction = p.get("dir") if p.get("dir") in ("asc", "desc") else "desc"
    return {"cols": list(cols), "sort": sort, "dir": direction}


def set_keyword_page_prefs(
    db: Session,
    user: User,
    *,
    cols: list[str] | None = None,
    sort: str | None = None,
    direction: str | None = None,
) -> None:
    prefs = load_prefs(user)
    kp = prefs.get("keyword_page") or {}
    if cols is not None:
        kp["cols"] = list(cols)
    if sort is not None and sort in KEYWORD_PAGE_SORT_KEYS:
        kp["sort"] = sort
    if direction in ("asc", "desc"):
        kp["dir"] = direction
    prefs["keyword_page"] = kp
    save_prefs(db, user, prefs)
