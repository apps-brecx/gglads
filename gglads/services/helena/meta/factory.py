"""Provider selection + access-mode enforcement.

get_meta_provider(db) is the single entry point the rest of the app uses. It
picks the backend from META_EXECUTION_MODE and wraps it in an AccessModeGuard
so a read-only integration can NEVER trigger a publish or spend action, even
if the chat agent asks for one. Reads are always allowed once connected.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from gglads.config import get_settings
from gglads.services import integrations as integrations_svc
from gglads.services.helena.meta.browser_agent import BrowserAgentMetaProvider
from gglads.services.helena.meta.meta_api import MetaApiProvider
from gglads.services.helena.meta.provider import MetaExecutionProvider
from gglads.services.helena.specs import (
    CampaignSpec,
    DateRange,
    InstagramPostSpec,
    ProviderResult,
)


def _denied(platform: str, reason: str) -> ProviderResult:
    return ProviderResult(
        success=False,
        message=f"Action blocked: {platform} integration is {reason}.",
    )


class AccessModeGuard(MetaExecutionProvider):
    """Wraps a concrete provider and enforces per-platform access mode.

    'meta_ads' governs campaign/budget actions; 'instagram' governs posts.
    A write through a read-only (or not-connected) integration is refused
    before the inner provider is ever called.
    """

    def __init__(self, inner: MetaExecutionProvider, db: Session) -> None:
        self._inner = inner
        self._db = db
        self.backend = inner.backend

    def _can_write(self, platform: str) -> tuple[bool, str]:
        row = integrations_svc.get_row(self._db, platform)
        if row is None or row.status != "connected":
            return False, "not connected"
        if row.access_mode != "read_write":
            return False, "set to Read Only"
        return True, ""

    def _can_read(self, platform: str) -> tuple[bool, str]:
        row = integrations_svc.get_row(self._db, platform)
        if row is None or row.status not in ("connected", "reconnect_required"):
            return False, "not connected"
        return True, ""

    # ---- campaigns (meta_ads, write = spend) --------------------------
    def create_campaign(self, spec: CampaignSpec) -> ProviderResult:
        ok, why = self._can_write("meta_ads")
        return self._inner.create_campaign(spec) if ok else _denied("Meta Ads", why)

    def update_budget(self, campaign_id: str, amount_cents: int) -> ProviderResult:
        ok, why = self._can_write("meta_ads")
        return self._inner.update_budget(campaign_id, amount_cents) if ok else _denied("Meta Ads", why)

    def pause_campaign(self, campaign_id: str) -> ProviderResult:
        ok, why = self._can_write("meta_ads")
        return self._inner.pause_campaign(campaign_id) if ok else _denied("Meta Ads", why)

    def resume_campaign(self, campaign_id: str) -> ProviderResult:
        ok, why = self._can_write("meta_ads")
        return self._inner.resume_campaign(campaign_id) if ok else _denied("Meta Ads", why)

    # ---- instagram posts (write = publish) ----------------------------
    def publish_instagram_post(self, post: InstagramPostSpec) -> ProviderResult:
        ok, why = self._can_write("instagram")
        return self._inner.publish_instagram_post(post) if ok else _denied("Instagram", why)

    def schedule_post(self, post: InstagramPostSpec, when: datetime) -> ProviderResult:
        ok, why = self._can_write("instagram")
        return self._inner.schedule_post(post, when) if ok else _denied("Instagram", why)

    # ---- reads --------------------------------------------------------
    def fetch_campaign_metrics(self, date_range: DateRange) -> ProviderResult:
        ok, why = self._can_read("meta_ads")
        return self._inner.fetch_campaign_metrics(date_range) if ok else _denied("Meta Ads", why)

    def fetch_instagram_insights(self, date_range: DateRange) -> ProviderResult:
        ok, why = self._can_read("instagram")
        return self._inner.fetch_instagram_insights(date_range) if ok else _denied("Instagram", why)

    def fetch_instagram_media(self, limit: int = 10) -> ProviderResult:
        ok, why = self._can_read("instagram")
        return self._inner.fetch_instagram_media(limit) if ok else _denied("Instagram", why)


def _base_provider(db: Session) -> MetaExecutionProvider:
    mode = (get_settings().meta_execution_mode or "browser").strip().lower()
    return MetaApiProvider(db) if mode == "api" else BrowserAgentMetaProvider()


def get_meta_provider(db: Session) -> MetaExecutionProvider:
    """The active, access-mode-guarded Meta provider."""
    return AccessModeGuard(_base_provider(db), db)
