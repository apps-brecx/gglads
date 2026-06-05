"""ShopifyApiEmailProvider — uses the existing Shopify OAuth connection.

NOTE: Shopify Email campaigns are not currently exposed by the public Admin
API for create/schedule/send (only some marketing-activity reporting exists).
This provider is therefore a thin, clearly-marked stub: it verifies the
Shopify connection and otherwise defers to the browser-agent path. Fill in the
calls below if/when the endpoints become available. Selected by
EMAIL_DELIVERY_MODE=api.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from gglads.services import integrations as integrations_svc
from gglads.services.helena.email.provider import EmailDeliveryProvider
from gglads.services.helena.specs import (
    DateRange,
    EmailCampaignSpec,
    ProviderResult,
)


class ShopifyApiEmailProvider(EmailDeliveryProvider):
    backend = "api"

    def __init__(self, db: Session) -> None:
        self._db = db

    def _shopify_ready(self) -> bool:
        cfg = integrations_svc.get_config(self._db, "shopify")
        return bool((cfg.get("store_domain") or "").strip() and (cfg.get("admin_api_token") or "").strip())

    def create_draft_campaign(self, campaign: EmailCampaignSpec) -> ProviderResult:
        if not self._shopify_ready():
            return ProviderResult(success=False, message="Shopify is not connected.")
        # TODO: POST the marketing/email campaign once Shopify exposes a
        # create endpoint. Until then, the browser-agent provider performs the
        # actual draft creation in the Shopify Email UI.
        return ProviderResult(
            success=False,
            message=(
                "Shopify Email create-campaign API is not available. "
                "Set EMAIL_DELIVERY_MODE=browser to use the browser agent."
            ),
        )

    def schedule_campaign(self, campaign: EmailCampaignSpec, when: datetime) -> ProviderResult:
        return self.create_draft_campaign(campaign)

    def fetch_email_metrics(self, date_range: DateRange) -> ProviderResult:
        # TODO: pull marketing-activity / engagement reporting via the Admin
        # API where available.
        return ProviderResult(
            success=False,
            message="Shopify Email metrics API not wired yet.",
        )
