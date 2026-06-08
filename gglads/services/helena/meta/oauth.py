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


def _resolve_ig_for_page(page_id: str | None, page_token: str | None) -> tuple[str | None, str | None]:
    """Resolve the Instagram account linked to a Page, using the PAGE token and
    trying both edges (instagram_business_account, then connected_instagram_account).
    The user token often returns these blank; the page token resolves them."""
    if not page_id or not page_token:
        return None, None
    try:
        r = httpx.get(f"{graph_base()}/{page_id}", params={
            "fields": "instagram_business_account{id,username},connected_instagram_account{id,username}",
            "access_token": page_token,
        }, timeout=20.0)
        if r.status_code == 200:
            d = r.json()
            ig = d.get("instagram_business_account") or d.get("connected_instagram_account") or {}
            return ig.get("id"), ig.get("username")
        logger.warning("IG resolve for page %s HTTP %s: %s", page_id, r.status_code, r.text[:200])
    except httpx.HTTPError as exc:
        logger.warning("IG resolve for page %s failed: %s", page_id, exc)
    return None, None


def _discover(token: str) -> dict[str, Any]:
    """Discover Pages + linked IG business accounts + ad accounts."""
    out: dict[str, Any] = {"pages": [], "ad_accounts": []}
    try:
        r = httpx.get(f"{graph_base()}/me/accounts", params={
            "fields": "name,access_token,instagram_business_account{id,username},"
                      "connected_instagram_account{id,username}",
            "access_token": token, "limit": 100,
        }, timeout=20.0)
        if r.status_code == 200:
            for p in r.json().get("data", []):
                ig = (p.get("instagram_business_account")
                      or p.get("connected_instagram_account") or {})
                ig_id, ig_user = ig.get("id"), ig.get("username")
                # Fall back to a per-page lookup with the page token if blank.
                if not ig_id:
                    ig_id, ig_user = _resolve_ig_for_page(p.get("id"), p.get("access_token"))
                out["pages"].append({
                    "page_id": p.get("id"), "page_name": p.get("name"),
                    "page_token": p.get("access_token"),
                    "ig_user_id": ig_id, "ig_username": ig_user,
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


def set_selection(
    db: Session, *, ad_account_id: str | None = None, page_id: str | None = None,
    user_id: int | None = None,
) -> tuple[bool, str]:
    """Change which ad account / Page (and its linked Instagram) Helena uses,
    without reconnecting. Picks from the already-discovered lists."""
    cfg = get_meta_config(db)
    if not cfg:
        return False, "Meta isn't connected."
    detail = []
    if ad_account_id is not None:
        match = next((a for a in cfg.get("ad_accounts", [])
                      if str(a.get("account_id")) == str(ad_account_id)), None)
        if match is None:
            return False, "That ad account isn't in your connected accounts."
        cfg["ad_account_id"] = match["account_id"]
        detail.append(f"Ad account: {match.get('name') or match['account_id']}")
    if page_id is not None:
        page = next((p for p in cfg.get("pages", [])
                     if str(p.get("page_id")) == str(page_id)), None)
        if page is None:
            return False, "That Page isn't in your connected accounts."
        # Resolve the linked Instagram account now if it wasn't captured before.
        if not page.get("ig_user_id") and page.get("page_token"):
            ig_id, ig_user = _resolve_ig_for_page(page["page_id"], page["page_token"])
            page["ig_user_id"], page["ig_username"] = ig_id, ig_user
        cfg["page_id"] = page["page_id"]
        cfg["page_token"] = page.get("page_token")
        cfg["ig_user_id"] = page.get("ig_user_id")
        cfg["ig_username"] = page.get("ig_username")
        detail.append(f"Page: {page.get('page_name')}")
        if page.get("ig_username"):
            detail.append(f"Instagram: @{page['ig_username']}")
        else:
            detail.append("No Instagram business account is linked to this Page")
    save_meta_config(db, cfg, user_id=user_id)
    ig = {"ig_username": cfg.get("ig_username"), "ig_user_id": cfg.get("ig_user_id"),
          "page_id": cfg.get("page_id"), "page_token": cfg.get("page_token")} \
        if cfg.get("ig_user_id") else None
    ad = next((a for a in cfg.get("ad_accounts", [])
               if str(a.get("account_id")) == str(cfg.get("ad_account_id"))), None)
    # Put the selected Page first so its name shows on the Facebook Pages chip.
    pages = sorted(cfg.get("pages", []),
                   key=lambda p: str(p.get("page_id")) != str(cfg.get("page_id")))
    _mark_cards_connected(db, ig, pages, ad, user_id)
    return True, ("Selection saved. " + " · ".join(detail)) if detail else "Selection saved."


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
        # Refresh the chip(s) for this card to reflect the current selection.
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
