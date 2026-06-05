"""BrowserAgentEmailProvider — creates the campaign in the Shopify Email UI
via the Chrome browser agent (paste/import the generated HTML, set subject +
preheader, choose the audience segment, save as draft or schedule).

Used when Shopify's API doesn't expose the email/marketing endpoints we need.
Selected by EMAIL_DELIVERY_MODE=browser.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from gglads.config import get_settings
from gglads.services.helena.email.provider import EmailDeliveryProvider
from gglads.services.helena.specs import (
    DateRange,
    EmailCampaignSpec,
    ProviderResult,
)

logger = logging.getLogger("gglads.helena.email.browser_agent")


class BrowserAgentEmailProvider(EmailDeliveryProvider):
    backend = "browser"

    def __init__(self) -> None:
        s = get_settings()
        self._url = (s.browser_agent_url or "").rstrip("/")
        self._token = s.browser_agent_token or ""

    def _run_task(self, action: str, goal: str, data: dict[str, Any]) -> ProviderResult:
        if not self._url:
            return ProviderResult(
                success=False,
                message="Browser agent is not configured (BROWSER_AGENT_URL).",
            )
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            resp = httpx.post(
                f"{self._url}/tasks",
                json={
                    "action": action,
                    "surface": "shopify_email",
                    "goal": goal,
                    "data": data,
                    "use_authenticated_session": True,
                },
                headers=headers,
                timeout=180.0,
            )
        except httpx.HTTPError as exc:
            return ProviderResult(success=False, message=f"Browser agent failed: {exc}")
        if resp.status_code != 200:
            return ProviderResult(success=False, message=f"HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            body = resp.json()
        except ValueError:
            return ProviderResult(success=False, message="Non-JSON response from browser agent.")
        return ProviderResult(
            success=bool(body.get("success")),
            external_id=body.get("external_id"),
            permalink=body.get("permalink"),
            message=body.get("message", ""),
            metrics=body.get("metrics", []) or [],
            steps=body.get("steps", []) or [],
            artifacts=body.get("artifacts", []) or [],
        )

    def create_draft_campaign(self, campaign: EmailCampaignSpec) -> ProviderResult:
        return self._run_task(
            "createDraftCampaign",
            f"Create a DRAFT Shopify Email campaign '{campaign.name}'. Do NOT send.",
            campaign.model_dump(),
        )

    def schedule_campaign(self, campaign: EmailCampaignSpec, when: datetime) -> ProviderResult:
        data = campaign.model_dump()
        data["scheduled_at"] = when.isoformat()
        return self._run_task(
            "scheduleCampaign",
            f"Create and schedule a Shopify Email campaign for {when.isoformat()}.",
            data,
        )

    def fetch_email_metrics(self, date_range: DateRange) -> ProviderResult:
        return self._run_task(
            "fetchEmailMetrics",
            "Read Shopify Email campaign metrics for the date range.",
            {"start": date_range.start.isoformat(), "end": date_range.end.isoformat()},
        )
