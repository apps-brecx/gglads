"""EmailDeliveryProvider — the swappable email push backend.

Two implementations behind it (selected by EMAIL_DELIVERY_MODE):
  - ShopifyApiEmailProvider: uses the existing Shopify OAuth connection if the
    needed marketing/email endpoints are available.
  - BrowserAgentEmailProvider: drives the Chrome browser agent to create the
    campaign in the Shopify Email UI.

Always creates a draft (or scheduled) campaign — never auto-sends. Sending to a
real list is a separate, explicitly-approved action.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from gglads.services.helena.specs import (
    DateRange,
    EmailCampaignSpec,
    ProviderResult,
)


class EmailDeliveryProvider(ABC):
    backend: str = "abstract"

    @abstractmethod
    def create_draft_campaign(self, campaign: EmailCampaignSpec) -> ProviderResult: ...

    @abstractmethod
    def schedule_campaign(self, campaign: EmailCampaignSpec, when: datetime) -> ProviderResult: ...

    @abstractmethod
    def fetch_email_metrics(self, date_range: DateRange) -> ProviderResult: ...
