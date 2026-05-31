"""Google Search Console — Search Analytics API for organic queries."""

from __future__ import annotations

import logging
import urllib.parse
from datetime import date, timedelta

import httpx
from sqlalchemy.orm import Session

from gglads.services import integrations as integrations_svc

logger = logging.getLogger("gglads.sc")


def normalize_site_url(site_url: str) -> str:
    """Accept bare-domain input and treat it as a Search Console domain property.

    Returns the value that should be sent to the API:
      'syruvia.com'              -> 'sc-domain:syruvia.com'
      'sc-domain:syruvia.com'    -> unchanged
      'https://syruvia.com/'     -> unchanged
    """
    s = (site_url or "").strip()
    if not s:
        return s
    if s.startswith("sc-domain:") or s.startswith("http://") or s.startswith("https://"):
        return s
    return f"sc-domain:{s}"


def page_url_from_site(site_url: str, handle: str) -> str | None:
    """Compose a public Shopify product URL suitable for a SC `page` filter."""
    s = (site_url or "").strip().rstrip("/")
    if not s:
        return None
    if s.startswith("sc-domain:"):
        domain = s[len("sc-domain:"):]
        return f"https://{domain}/products/{handle}"
    if s.startswith("http://") or s.startswith("https://"):
        return f"{s}/products/{handle}"
    return f"https://{s}/products/{handle}"


def _refresh_access_token(cfg: dict) -> tuple[str | None, str | None]:
    try:
        r = httpx.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": cfg["oauth_client_id"].strip(),
                "client_secret": cfg["oauth_client_secret"].strip(),
                "refresh_token": cfg["refresh_token"].strip(),
                "grant_type": "refresh_token",
            },
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if r.status_code != 200:
        return None, f"Token refresh HTTP {r.status_code}: {r.text[:200]}"
    token = r.json().get("access_token")
    if not token:
        return None, "Token refresh returned no access_token."
    return token, None


def get_queries_for_page(
    db: Session, page_url: str, days: int = 90, row_limit: int = 100
) -> tuple[list[dict] | None, str | None]:
    cfg = integrations_svc.get_config(db, "google_search_console")
    required = ["site_url", "oauth_client_id", "oauth_client_secret", "refresh_token"]
    missing = [k for k in required if not (cfg.get(k) or "").strip()]
    if missing:
        return None, f"Search Console missing: {', '.join(missing)}"

    token, err = _refresh_access_token(cfg)
    if err:
        return None, err

    site_url = normalize_site_url(cfg["site_url"])
    end = date.today()
    start = end - timedelta(days=days)
    url = (
        "https://www.googleapis.com/webmasters/v3/sites/"
        f"{urllib.parse.quote(site_url, safe='')}/searchAnalytics/query"
    )
    body = {
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "dimensions": ["query"],
        "rowLimit": row_limit,
        "dimensionFilterGroups": [
            {
                "filters": [
                    {
                        "dimension": "page",
                        "operator": "equals",
                        "expression": page_url,
                    }
                ]
            }
        ],
    }
    try:
        resp = httpx.post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}: {resp.text[:200]}"
    rows = resp.json().get("rows", [])
    return [
        {
            "query": row["keys"][0],
            "clicks": int(row.get("clicks", 0)),
            "impressions": int(row.get("impressions", 0)),
            "ctr": float(row.get("ctr", 0.0)),
            "position": float(row.get("position", 0.0)),
        }
        for row in rows
    ], None
