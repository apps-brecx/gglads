"""BrowserAgentMetaProvider — the provider we use now.

Drives the Claude-controlled Chrome browser agent to perform each action in
the Meta Ads Manager / Instagram web UI and read the resulting confirmation
or metrics back. It accepts a structured task spec, asks the browser agent to
execute the steps against an already-authenticated session (a human performs
login/verification — we never script credentials), and returns a normalized
ProviderResult.

The agent itself is an external service reached over HTTP at
settings.browser_agent_url. This class is responsible for (1) translating a
typed spec into a concrete, ordered browser task, (2) invoking the agent, and
(3) normalizing whatever it returns. If the agent is not configured we fail
closed with a clear message rather than silently no-op.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from gglads.config import get_settings
from gglads.services.helena.meta.provider import MetaExecutionProvider
from gglads.services.helena.specs import (
    CampaignSpec,
    DateRange,
    InstagramPostSpec,
    ProviderResult,
)

logger = logging.getLogger("gglads.helena.browser_agent")


class BrowserAgentMetaProvider(MetaExecutionProvider):
    backend = "browser"

    def __init__(self) -> None:
        s = get_settings()
        self._url = (s.browser_agent_url or "").rstrip("/")
        self._token = s.browser_agent_token or ""

    # ---- core dispatch -------------------------------------------------
    def _run_task(
        self,
        action: str,
        surface: str,
        goal: str,
        data: dict[str, Any],
    ) -> ProviderResult:
        """Send one browser task to the agent and normalize the response.

        The task is a structured instruction the Claude browser agent
        executes step-by-step against the authenticated Meta/Instagram UI.
        """
        if not self._url:
            return ProviderResult(
                success=False,
                message=(
                    "Browser agent is not configured (BROWSER_AGENT_URL). "
                    "Connect the browser agent on the Integrations page."
                ),
                steps=[{"step": "preflight", "ok": False, "detail": "no agent url"}],
            )
        payload = {
            "action": action,
            "surface": surface,  # 'meta_ads' | 'instagram'
            "goal": goal,
            "data": data,
            # Operate the already-authenticated session; never log in.
            "use_authenticated_session": True,
        }
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            resp = httpx.post(
                f"{self._url}/tasks",
                json=payload,
                headers=headers,
                timeout=180.0,
            )
        except httpx.HTTPError as exc:
            return ProviderResult(
                success=False,
                message=f"Browser agent request failed: {type(exc).__name__}: {exc}",
                steps=[{"step": "dispatch", "ok": False, "detail": str(exc)}],
            )
        if resp.status_code != 200:
            return ProviderResult(
                success=False,
                message=f"Browser agent HTTP {resp.status_code}: {resp.text[:300]}",
            )
        try:
            body = resp.json()
        except ValueError:
            return ProviderResult(success=False, message="Browser agent returned non-JSON.")
        return ProviderResult(
            success=bool(body.get("success")),
            external_id=body.get("external_id"),
            permalink=body.get("permalink"),
            message=body.get("message", ""),
            metrics=body.get("metrics", []) or [],
            steps=body.get("steps", []) or [],
            artifacts=body.get("artifacts", []) or [],
        )

    # ---- campaigns -----------------------------------------------------
    def create_campaign(self, spec: CampaignSpec) -> ProviderResult:
        return self._run_task(
            "createCampaign",
            "meta_ads",
            f"Create a {spec.objective} campaign '{spec.name}' with a "
            f"{spec.budget_type} budget of {spec.budget_cents/100:.2f}.",
            spec.model_dump(),
        )

    def update_budget(self, campaign_id: str, amount_cents: int) -> ProviderResult:
        return self._run_task(
            "updateBudget",
            "meta_ads",
            f"Set campaign {campaign_id} budget to {amount_cents/100:.2f}.",
            {"campaign_id": campaign_id, "amount_cents": amount_cents},
        )

    def pause_campaign(self, campaign_id: str) -> ProviderResult:
        return self._run_task(
            "pauseCampaign", "meta_ads",
            f"Pause campaign {campaign_id}.",
            {"campaign_id": campaign_id},
        )

    def resume_campaign(self, campaign_id: str) -> ProviderResult:
        return self._run_task(
            "resumeCampaign", "meta_ads",
            f"Resume campaign {campaign_id}.",
            {"campaign_id": campaign_id},
        )

    # ---- instagram posts ----------------------------------------------
    def publish_instagram_post(self, post: InstagramPostSpec) -> ProviderResult:
        return self._run_task(
            "publishInstagramPost", "instagram",
            "Publish an Instagram feed post with the supplied image and caption.",
            post.model_dump(),
        )

    def schedule_post(self, post: InstagramPostSpec, when: datetime) -> ProviderResult:
        data = post.model_dump()
        data["scheduled_at"] = when.isoformat()
        return self._run_task(
            "schedulePost", "instagram",
            f"Schedule an Instagram post for {when.isoformat()}.",
            data,
        )

    # ---- read-back -----------------------------------------------------
    def fetch_campaign_metrics(self, date_range: DateRange) -> ProviderResult:
        return self._run_task(
            "fetchCampaignMetrics", "meta_ads",
            "Read campaign/ad-set performance metrics for the date range.",
            {"start": date_range.start.isoformat(), "end": date_range.end.isoformat()},
        )

    def fetch_instagram_insights(self, date_range: DateRange) -> ProviderResult:
        return self._run_task(
            "fetchInstagramInsights", "instagram",
            "Read Instagram Insights for the date range.",
            {"start": date_range.start.isoformat(), "end": date_range.end.isoformat()},
        )
