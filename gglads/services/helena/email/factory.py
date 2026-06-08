"""Email provider selection + access-mode enforcement.

Shopify Email is surfaced through the existing Shopify integration, so the
access-mode toggle on the Shopify card governs email too: Read Only = read
metrics only; Read & Write = create draft/scheduled campaigns (still gated by
approval before any send).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from gglads.config import get_settings
from gglads.services import integrations as integrations_svc
from gglads.services.helena.email.browser_agent import BrowserAgentEmailProvider
from gglads.services.helena.email.provider import EmailDeliveryProvider
from gglads.services.helena.email.shopify_api import ShopifyApiEmailProvider
from gglads.services.helena.specs import (
    DateRange,
    EmailCampaignSpec,
    ProviderResult,
)


def _denied(reason: str) -> ProviderResult:
    return ProviderResult(success=False, message=f"Action blocked: Shopify integration is {reason}.")


class EmailAccessModeGuard(EmailDeliveryProvider):
    def __init__(self, inner: EmailDeliveryProvider, db: Session) -> None:
        self._inner = inner
        self._db = db
        self.backend = inner.backend

    def _can_write(self) -> tuple[bool, str]:
        row = integrations_svc.get_row(self._db, "shopify")
        # Shopify may be configured via env (no row); treat configured as connected.
        cfg = integrations_svc.get_config(self._db, "shopify")
        connected = bool((cfg.get("store_domain") or "").strip())
        if not connected:
            return False, "not connected"
        if row is not None and row.access_mode != "read_write":
            return False, "set to Read Only"
        return True, ""

    def create_draft_campaign(self, campaign: EmailCampaignSpec) -> ProviderResult:
        ok, why = self._can_write()
        return self._inner.create_draft_campaign(campaign) if ok else _denied(why)

    def schedule_campaign(self, campaign: EmailCampaignSpec, when: datetime) -> ProviderResult:
        ok, why = self._can_write()
        return self._inner.schedule_campaign(campaign, when) if ok else _denied(why)

    def fetch_email_metrics(self, date_range: DateRange) -> ProviderResult:
        return self._inner.fetch_email_metrics(date_range)


def get_email_provider(db: Session) -> EmailDeliveryProvider:
    mode = (get_settings().email_delivery_mode or "browser").strip().lower()
    inner: EmailDeliveryProvider = (
        ShopifyApiEmailProvider(db) if mode == "api" else BrowserAgentEmailProvider()
    )
    return EmailAccessModeGuard(inner, db)
