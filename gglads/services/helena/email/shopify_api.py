"""ShopifyApiEmailProvider — the EMAIL_DELIVERY_MODE="api" provider.

We send marketing email exclusively through Shopify Email. Shopify's public
Admin API does NOT expose an endpoint to create a sendable Shopify Email
campaign with custom HTML (only external marketing-activity reporting exists),
so the only real way to create the draft inside Shopify is to operate the
Shopify Email UI. This provider therefore performs creation/scheduling by
delegating to the browser agent (which drives Shopify Email), while keeping a
single clear seam for the day Shopify ships a create endpoint.

It always creates a DRAFT and never sends. Selected by EMAIL_DELIVERY_MODE=api;
the default EMAIL_DELIVERY_MODE=browser uses BrowserAgentEmailProvider directly.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from gglads.services import integrations as integrations_svc
from gglads.services.helena.email.browser_agent import BrowserAgentEmailProvider
from gglads.services.helena.email.provider import EmailDeliveryProvider
from gglads.services.helena.specs import (
    DateRange,
    EmailCampaignSpec,
    ProviderResult,
)

logger = logging.getLogger("gglads.helena.email.shopify_api")


class ShopifyApiEmailProvider(EmailDeliveryProvider):
    backend = "api"

    def __init__(self, db: Session) -> None:
        self._db = db
        self._browser = BrowserAgentEmailProvider()

    def _shopify_ready(self) -> bool:
        cfg = integrations_svc.get_config(self._db, "shopify")
        return bool(
            (cfg.get("store_domain") or "").strip()
            and (cfg.get("admin_api_token") or "").strip()
        )

    def create_draft_campaign(self, campaign: EmailCampaignSpec) -> ProviderResult:
        if not self._shopify_ready():
            return ProviderResult(success=False, message="Shopify is not connected.")
        # No public Admin API to create a Shopify Email draft — create it in
        # the Shopify Email UI via the browser agent. Still draft-only.
        # TODO: switch to a native Admin API mutation if Shopify ships one.
        return self._browser.create_draft_campaign(campaign)

    def schedule_campaign(self, campaign: EmailCampaignSpec, when: datetime) -> ProviderResult:
        if not self._shopify_ready():
            return ProviderResult(success=False, message="Shopify is not connected.")
        return self._browser.schedule_campaign(campaign, when)

    def fetch_email_metrics(self, date_range: DateRange) -> ProviderResult:
        # Engagement read-back also goes through the browser agent today.
        return self._browser.fetch_email_metrics(date_range)
