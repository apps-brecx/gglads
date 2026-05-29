"""Google Ads Keyword Planner client — generate_keyword_ideas only."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from gglads.services import integrations as integrations_svc

logger = logging.getLogger("gglads.kp")

# English / United States — most relevant for our brand.
DEFAULT_LANGUAGE_ID = "1000"
DEFAULT_GEO_TARGET_ID = "2840"


def _build_client(db: Session):
    cfg = integrations_svc.get_config(db, "google_ads")
    required = ["developer_token", "oauth_client_id", "oauth_client_secret", "refresh_token", "customer_id"]
    missing = [k for k in required if not (cfg.get(k) or "").strip()]
    if missing:
        return None, None, f"Google Ads missing: {', '.join(missing)}"
    try:
        from google.ads.googleads.client import GoogleAdsClient
    except ImportError:
        return None, None, "google-ads SDK not installed"
    client_cfg: dict[str, Any] = {
        "developer_token": cfg["developer_token"].strip(),
        "client_id": cfg["oauth_client_id"].strip(),
        "client_secret": cfg["oauth_client_secret"].strip(),
        "refresh_token": cfg["refresh_token"].strip(),
        "use_proto_plus": True,
    }
    login_cid = (cfg.get("login_customer_id") or "").replace("-", "").strip()
    if login_cid:
        client_cfg["login_customer_id"] = login_cid
    try:
        client = GoogleAdsClient.load_from_dict(client_cfg)
    except Exception as exc:  # noqa: BLE001
        return None, None, f"{type(exc).__name__}: {exc}"
    customer_id = cfg["customer_id"].replace("-", "").strip()
    return client, customer_id, None


def generate_keyword_ideas(db: Session, seed_keywords: list[str]) -> tuple[list[dict] | None, str | None]:
    """Call KeywordPlanIdeaService.generate_keyword_ideas. Returns (rows, error)."""
    seeds = [s.strip() for s in seed_keywords if s and s.strip()]
    if not seeds:
        return [], None
    client, customer_id, err = _build_client(db)
    if err:
        return None, err
    try:
        service = client.get_service("KeywordPlanIdeaService")
        request = client.get_type("GenerateKeywordIdeasRequest")
        request.customer_id = customer_id
        request.language = f"languageConstants/{DEFAULT_LANGUAGE_ID}"
        request.geo_target_constants.append(
            f"geoTargetConstants/{DEFAULT_GEO_TARGET_ID}"
        )
        request.include_adult_keywords = False
        request.keyword_seed.keywords.extend(seeds[:20])  # KP accepts up to 20
        response = service.generate_keyword_ideas(request=request)
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"

    rows: list[dict] = []
    for idea in response:
        m = idea.keyword_idea_metrics
        rows.append(
            {
                "keyword": idea.text,
                "avg_monthly_searches": int(getattr(m, "avg_monthly_searches", 0) or 0),
                "competition": str(getattr(m, "competition", "")).split(".")[-1].lower() or None,
                "low_bid_micros": int(
                    getattr(m, "low_top_of_page_bid_micros", 0) or 0
                ),
                "high_bid_micros": int(
                    getattr(m, "high_top_of_page_bid_micros", 0) or 0
                ),
            }
        )
    return rows, None
