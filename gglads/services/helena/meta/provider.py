"""The swappable execution backend.

`MetaExecutionProvider` is the ONLY surface the chat agent, skills, task
queue, and dashboards touch for Meta/Instagram work. Two implementations sit
behind it (BrowserAgentMetaProvider now, MetaApiProvider later), selected by
the META_EXECUTION_MODE config flag via factory.get_meta_provider().

Everything else in the app is intentionally ignorant of which backend runs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from gglads.services.helena.specs import (
    CampaignSpec,
    DateRange,
    InstagramPostSpec,
    ProviderResult,
)


class MetaExecutionProvider(ABC):
    """Backend-agnostic contract for all Meta Ads + Instagram operations."""

    # ---- name for logging / ExecutionRun.backend ----------------------
    backend: str = "abstract"

    # ---- Ad campaigns -------------------------------------------------
    @abstractmethod
    def create_campaign(self, spec: CampaignSpec) -> ProviderResult: ...

    @abstractmethod
    def update_budget(self, campaign_id: str, amount_cents: int) -> ProviderResult: ...

    @abstractmethod
    def pause_campaign(self, campaign_id: str) -> ProviderResult: ...

    @abstractmethod
    def resume_campaign(self, campaign_id: str) -> ProviderResult: ...

    # ---- Instagram posts ----------------------------------------------
    @abstractmethod
    def publish_instagram_post(self, post: InstagramPostSpec) -> ProviderResult: ...

    @abstractmethod
    def schedule_post(self, post: InstagramPostSpec, when: datetime) -> ProviderResult: ...

    # ---- Read-back ----------------------------------------------------
    @abstractmethod
    def fetch_campaign_metrics(self, date_range: DateRange) -> ProviderResult: ...

    @abstractmethod
    def fetch_instagram_insights(self, date_range: DateRange) -> ProviderResult: ...

    # ---- Organic Instagram post performance ---------------------------
    # Concrete default so backends that can't read post insights don't have to
    # implement it; MetaApiProvider overrides with the real Graph API call.
    def fetch_instagram_media(self, limit: int = 10) -> ProviderResult:
        return ProviderResult(
            success=False,
            message="Reading Instagram post insights isn't supported by this backend.",
        )


# Imported late to keep the type hint above readable.
from datetime import datetime  # noqa: E402
