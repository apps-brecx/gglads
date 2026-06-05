"""MetaApiProvider — STUB for when we have official push access.

Same MetaExecutionProvider interface as BrowserAgentMetaProvider. Once
META_APP_ID / META_APP_SECRET / INSTAGRAM_* are populated and approved, set
META_EXECUTION_MODE=api and fill in the calls below. The card UI, chat agent,
skills, task queue, and dashboards do not change — only this file does.
"""

from __future__ import annotations

from datetime import datetime

from gglads.services.helena.meta.provider import MetaExecutionProvider
from gglads.services.helena.specs import (
    CampaignSpec,
    DateRange,
    InstagramPostSpec,
    ProviderResult,
)

_NOT_READY = ProviderResult(
    success=False,
    message=(
        "MetaApiProvider is not yet implemented — we do not have Meta "
        "Marketing API / Instagram Graph API push access. Use "
        "META_EXECUTION_MODE=browser until access is granted."
    ),
)


class MetaApiProvider(MetaExecutionProvider):
    backend = "api"

    def create_campaign(self, spec: CampaignSpec) -> ProviderResult:
        # TODO: POST /act_<account_id>/campaigns via the Marketing API, then
        # create ad set + ad with the creative. Map the returned id to
        # ProviderResult.external_id.
        return _NOT_READY

    def update_budget(self, campaign_id: str, amount_cents: int) -> ProviderResult:
        # TODO: POST /<campaign_id> with daily_budget / lifetime_budget.
        return _NOT_READY

    def pause_campaign(self, campaign_id: str) -> ProviderResult:
        # TODO: POST /<campaign_id> status=PAUSED.
        return _NOT_READY

    def resume_campaign(self, campaign_id: str) -> ProviderResult:
        # TODO: POST /<campaign_id> status=ACTIVE.
        return _NOT_READY

    def publish_instagram_post(self, post: InstagramPostSpec) -> ProviderResult:
        # TODO: Instagram Graph API — create media container then publish.
        return _NOT_READY

    def schedule_post(self, post: InstagramPostSpec, when: datetime) -> ProviderResult:
        # TODO: Graph API scheduled publishing (or Creator Studio successor).
        return _NOT_READY

    def fetch_campaign_metrics(self, date_range: DateRange) -> ProviderResult:
        # TODO: GET /<id>/insights with the date range + fields.
        return _NOT_READY

    def fetch_instagram_insights(self, date_range: DateRange) -> ProviderResult:
        # TODO: GET /<ig_user_id>/insights.
        return _NOT_READY
