"""Out-of-stock ad guard.

Matches every Meta ad to a Shopify product by the destination URL's product
handle. When that product is out of stock it PAUSES the ad (pausing never
spends, so it's safe to do automatically) and emails an alert; when stock
returns it AUTO-RESUMES the ads it paused. An admin can set `allow_oos` on an
ad to keep it running even while out of stock.

Run on a schedule by gglads/cron/ad_stock_guard.py.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from gglads.models.helena import AdStockGuardState
from gglads.models.shopify_product import ShopifyProduct
from gglads.models.user import User
from gglads.services import email as email_svc

logger = logging.getLogger("gglads.helena.ad_stock_guard")

_HANDLE_RE = re.compile(r"/products/([A-Za-z0-9][A-Za-z0-9\-_]*)")


def _now() -> datetime:
    return datetime.now(UTC)


def handle_from_url(url: str | None) -> str | None:
    """Extract the Shopify product handle from a /products/<handle> URL."""
    if not url:
        return None
    m = _HANDLE_RE.search(url)
    return m.group(1).lower() if m else None


def _state(db: Session, ad_id: str) -> AdStockGuardState:
    st = db.get(AdStockGuardState, ad_id)
    if st is None:
        st = AdStockGuardState(ad_id=ad_id)
        db.add(st)
    return st


def run_guard(db: Session) -> tuple[bool, str, dict[str, Any]]:
    """Check stock for every linked ad and pause/resume as needed. Returns
    (ok, detail, stats)."""
    from gglads.services.helena.meta.factory import get_meta_provider

    provider = get_meta_provider(db)
    res = provider.fetch_ads_with_links()
    if not res.get("ok"):
        return False, res.get("error", "Meta not connected."), {}

    paused: list[dict] = []
    resumed: list[dict] = []
    checked = matched = 0
    for ad in res.get("ads", []):
        handle = handle_from_url(ad.get("link"))
        if not handle:
            continue
        product = db.scalar(select(ShopifyProduct).where(ShopifyProduct.handle == handle))
        if product is None:
            continue
        matched += 1
        oos = (product.total_inventory or 0) <= 0
        st = _state(db, ad["ad_id"])
        st.ad_name = ad.get("ad_name")
        st.campaign_id = ad.get("campaign_id")
        st.product_handle = handle
        st.updated_at = _now()
        status = (ad.get("status") or "").upper()

        if oos:
            st.oos_since = st.oos_since or _now()
            if st.allow_oos:
                continue  # admin override — keep running while OOS
            if status == "ACTIVE":
                r = provider.set_status(ad["ad_id"], "PAUSED")
                if getattr(r, "success", False):
                    st.paused_by_guard = True
                    st.last_alert_at = _now()
                    paused.append({"ad": st.ad_name or ad["ad_id"], "product": product.title,
                                   "handle": handle})
                else:
                    logger.warning("guard couldn't pause %s: %s", ad["ad_id"],
                                   getattr(r, "message", ""))
        else:
            st.oos_since = None
            if st.paused_by_guard:
                r = provider.set_status(ad["ad_id"], "ACTIVE")
                if getattr(r, "success", False):
                    st.paused_by_guard = False
                    resumed.append({"ad": st.ad_name or ad["ad_id"], "product": product.title,
                                    "handle": handle})
                else:
                    logger.warning("guard couldn't resume %s: %s", ad["ad_id"],
                                   getattr(r, "message", ""))
        checked += 1

    db.commit()
    if paused or resumed:
        _send_alert(db, paused, resumed)
    stats = {"matched": matched, "checked": checked,
             "paused": len(paused), "resumed": len(resumed)}
    return True, (f"Stock guard: matched {matched} ad(s), paused {len(paused)}, "
                  f"resumed {len(resumed)}."), stats


def _admin_emails(db: Session) -> list[str]:
    rows = db.scalars(
        select(User).where(User.role == "admin", User.is_active.is_(True))
    ).all()
    return [u.email for u in rows if u.email]


def _send_alert(db: Session, paused: list[dict], resumed: list[dict]) -> None:
    if not email_svc.is_configured(db):
        logger.info("Stock-guard alert (no SMTP configured): paused=%s resumed=%s",
                    paused, resumed)
        return
    recipients = _admin_emails(db)
    if not recipients:
        return
    parts = []
    if paused:
        rows = "".join(f"<li><strong>{p['ad']}</strong> — {p['product']} "
                       f"(<code>{p['handle']}</code>)</li>" for p in paused)
        parts.append(f"<p><strong>Paused {len(paused)} ad(s)</strong> — out of stock:</p>"
                     f"<ul>{rows}</ul>")
    if resumed:
        rows = "".join(f"<li><strong>{p['ad']}</strong> — {p['product']} "
                       f"(<code>{p['handle']}</code>)</li>" for p in resumed)
        parts.append(f"<p><strong>Resumed {len(resumed)} ad(s)</strong> — back in stock:</p>"
                     f"<ul>{rows}</ul>")
    html = ("<div style=\"font-family:sans-serif\">"
            "<h2>Meta ad stock guard</h2>" + "".join(parts) +
            "<p style=\"color:#666;font-size:13px\">Manage overrides on the Meta Ads page. "
            "Pausing is automatic; auto-resume only restarts ads the guard paused.</p></div>")
    text = "Meta ad stock guard\n" + \
        "\n".join(f"PAUSED {p['ad']} — {p['product']}" for p in paused) + "\n" + \
        "\n".join(f"RESUMED {p['ad']} — {p['product']}" for p in resumed)
    subject = []
    if paused:
        subject.append(f"{len(paused)} ad(s) paused (out of stock)")
    if resumed:
        subject.append(f"{len(resumed)} resumed")
    subj = "Stock guard: " + ", ".join(subject)
    for to in recipients:
        ok, detail = email_svc.send_email(db, to, subj, html, text)
        if not ok:
            logger.warning("stock-guard email to %s failed: %s", to, detail)
