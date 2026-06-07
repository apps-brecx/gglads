"""Meta (Facebook Login) OAuth + connection storage for the official API path.

Flow: the Integrations "Connect" button (for instagram / facebook_pages /
meta_ads, when META_EXECUTION_MODE=api and app creds are set) sends the user to
the Facebook login dialog. Meta redirects back to the callback with a code; we
exchange it for a long-lived user token, discover the linked Page(s),
Instagram business account(s), and ad account(s), and store everything
(encrypted) in the 'meta' integration row. MetaApiProvider reads from there.

Requires a Meta Developer app with these permissions (app review needed before
they work on non-test accounts): instagram_basic, instagram_content_publish,
instagram_manage_insights, pages_show_list, pages_read_engagement,
pages_manage_posts, business_management, ads_management, ads_read.
"""

from __future__ import annotations

import logging
from datetime import UTC
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy.orm import Session

from gglads.config import get_settings
from gglads.models.integration import Integration
from gglads.services.crypto import decrypt_json, encrypt_json

logger = logging.getLogger("gglads.helena.meta.oauth")

SCOPES = [
    "instagram_basic",
    "instagram_content_publish",
    "instagram_manage_insights",
    "pages_show_list",
    "pages_read_engagement",
    "pages_manage_posts",
    "business_management",
    "ads_management",
    "ads_read",
]

# The three cards this single Meta connection powers.
META_PLATFORMS = ("instagram", "facebook_pages", "meta_ads")


def graph_base() -> str:
    return f"https://graph.facebook.com/{get_settings().meta_graph_version}"


def is_api_configured() -> bool:
    s = get_settings()
    return bool(s.meta_app_id and s.meta_app_secret and s.meta_oauth_redirect_uri)


# ---------------------------------------------------------------------------
# Stored connection ('meta' integration row)
# ---------------------------------------------------------------------------

def get_meta_config(db: Session) -> dict[str, Any]:
    row = db.get(Integration, "meta")
    if row is None:
        return {}
    return decrypt_json(row.config_encrypted) or {}


def save_meta_config(db: Session, data: dict[str, Any], user_id: int | None = None) -> None:
    from datetime import datetime
    row = db.get(Integration, "meta")
    enc = encrypt_json(data)
    if row is None:
        row = Integration(name="meta", config_encrypted=enc, status="connected",
                          auth_type="oauth", access_mode="read_write")
        db.add(row)
    else:
        row.config_encrypted = enc
        row.status = "connected"
        row.auth_type = "oauth"
    row.updated_by_user_id = user_id
    row.updated_at = datetime.now(UTC)
    db.commit()


# ---------------------------------------------------------------------------
# OAuth dialog + code exchange
# ---------------------------------------------------------------------------

def authorize_url(state: str) -> str:
    s = get_settings()
    params = {
        "client_id": s.meta_app_id,
        "redirect_uri": s.meta_oauth_redirect_uri,
        "state": state,
        "scope": ",".join(SCOPES),
        "response_type": "code",
    }
    return f"https://www.facebook.com/{s.meta_graph_version}/dialog/oauth?{urlencode(params)}"


def _exchange_code(code: str) -> tuple[str | None, str | None]:
    s = get_settings()
    try:
        r = httpx.get(f"{graph_base()}/oauth/access_token", params={
            "client_id": s.meta_app_id,
            "client_secret": s.meta_app_secret,
            "redirect_uri": s.meta_oauth_redirect_uri,
            "code": code,
        }, timeout=20.0)
    except httpx.HTTPError as exc:
        return None, f"Token exchange failed: {type(exc).__name__}: {exc}"
    if r.status_code != 200:
        return None, f"Token exchange HTTP {r.status_code}: {r.text[:300]}"
    short = r.json().get("access_token")
    if not short:
        return None, "No access_token returned."
    # Upgrade to a long-lived token (~60 days).
    try:
        r2 = httpx.get(f"{graph_base()}/oauth/access_token", params={
            "grant_type": "fb_exchange_token",
            "client_id": s.meta_app_id,
            "client_secret": s.meta_app_secret,
            "fb_exchange_token": short,
        }, timeout=20.0)
        if r2.status_code == 200 and r2.json().get("access_token"):
            return r2.json()["access_token"], None
    except httpx.HTTPError:
        pass
    return short, None  # fall back to short-lived if exchange fails


def _discover(token: str) -> dict[str, Any]:
    """Discover Pages + linked IG business accounts + ad accounts."""
    out: dict[str, Any] = {"pages": [], "ad_accounts": []}
    try:
        r = httpx.get(f"{graph_base()}/me/accounts", params={
            "fields": "name,access_token,instagram_business_account{id,username}",
            "access_token": token, "limit": 100,
        }, timeout=20.0)
        if r.status_code == 200:
            for p in r.json().get("data", []):
                ig = p.get("instagram_business_account") or {}
                out["pages"].append({
                    "page_id": p.get("id"), "page_name": p.get("name"),
                    "page_token": p.get("access_token"),
                    "ig_user_id": ig.get("id"), "ig_username": ig.get("username"),
                })
    except httpx.HTTPError as exc:
        logger.warning("page discovery failed: %s", exc)
    try:
        r = httpx.get(f"{graph_base()}/me/adaccounts", params={
            "fields": "name,account_id", "access_token": token, "limit": 100,
        }, timeout=20.0)
        if r.status_code == 200:
            for a in r.json().get("data", []):
                out["ad_accounts"].append({"id": a.get("id"), "account_id": a.get("account_id"),
                                           "name": a.get("name")})
    except httpx.HTTPError as exc:
        logger.warning("ad account discovery failed: %s", exc)
    return out


def complete_oauth(db: Session, code: str, user_id: int | None) -> tuple[bool, str]:
    """Exchange code, discover assets, persist, and mark the cards connected."""
    token, err = _exchange_code(code)
    if err:
        return False, err
    assets = _discover(token)
    pages = assets["pages"]
    ig = next((p for p in pages if p.get("ig_user_id")), None)
    ad = assets["ad_accounts"][0] if assets["ad_accounts"] else None
    save_meta_config(db, {
        "access_token": token,
        "pages": pages,
        "ad_accounts": assets["ad_accounts"],
        "ig_user_id": ig["ig_user_id"] if ig else None,
        "ig_username": ig["ig_username"] if ig else None,
        "page_id": ig["page_id"] if ig else (pages[0]["page_id"] if pages else None),
        "page_token": ig["page_token"] if ig else (pages[0]["page_token"] if pages else None),
        "ad_account_id": ad["account_id"] if ad else None,
    }, user_id=user_id)
    _mark_cards_connected(db, ig, pages, ad, user_id)
    detail = (
        f"Instagram: @{ig['ig_username']}" if ig else "No Instagram business account found"
    )
    if ad:
        detail += f" · Ad account: {ad.get('name') or ad.get('account_id')}"
    return True, detail


def _mark_cards_connected(db, ig, pages, ad, user_id) -> None:
    from datetime import datetime

    from gglads.models.integration import IntegrationAccount
    chips = {
        "instagram": (f"@{ig['ig_username']}", f"https://instagram.com/{ig['ig_username']}")
        if ig else None,
        "facebook_pages": (pages[0]["page_name"], None) if pages else None,
        "meta_ads": (ad.get("name") or ad.get("account_id"), None) if ad else None,
    }
    for key in META_PLATFORMS:
        row = db.get(Integration, key)
        if row is None:
            row = Integration(name=key, config_encrypted=encrypt_json({}))
            db.add(row)
        row.status = "connected"
        row.auth_type = "oauth"
        row.access_mode = "read_write"
        row.updated_by_user_id = user_id
        row.updated_at = datetime.now(UTC)
        # refresh chips for this card
        for acc in list(row.__dict__.get("accounts", []) or []):
            pass
        db.query(IntegrationAccount).filter(
            IntegrationAccount.integration_name == key
        ).delete()
        chip = chips.get(key)
        if chip:
            db.add(IntegrationAccount(integration_name=key, handle=chip[0],
                                      external_url=chip[1], status="connected"))
    db.commit()


def disconnect(db: Session) -> None:
    from gglads.models.integration import IntegrationAccount
    for key in (*META_PLATFORMS, "meta"):
        row = db.get(Integration, key)
        if row is not None:
            row.status = "not_connected"
        db.query(IntegrationAccount).filter(
            IntegrationAccount.integration_name == key
        ).delete()
    db.commit()
