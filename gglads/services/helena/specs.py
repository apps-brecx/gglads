"""Structured specs and normalized results passed across the provider
interfaces. Keeping these as Pydantic models means the chat agent, the task
queue, and both browser/API backends all speak the same shapes — so swapping
backends is a config change, never a code change at the call sites.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Meta / Instagram
# ---------------------------------------------------------------------------

class CampaignSpec(BaseModel):
    name: str
    objective: str = "traffic"
    budget_type: str = "daily"  # daily | lifetime
    budget_cents: int = 0
    audience: dict[str, Any] = Field(default_factory=dict)
    creative_image_url: str | None = None
    creative_copy: str | None = None
    account_handle: str | None = None


class InstagramPostSpec(BaseModel):
    caption: str = ""
    hashtags: str | None = None
    image_url: str | None = None
    account_handle: str | None = None


class DateRange(BaseModel):
    start: datetime
    end: datetime


class ProviderResult(BaseModel):
    """Normalized result every provider method returns."""

    success: bool
    external_id: str | None = None
    permalink: str | None = None
    message: str = ""
    # For fetch_* calls: list of {platform, entity_type, entity_id, metric,
    # value, captured_for} dicts.
    metrics: list[dict[str, Any]] = Field(default_factory=list)
    # Audit trail: ordered step records and screenshot/confirmation links.
    steps: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

class EmailCampaignSpec(BaseModel):
    name: str
    subject: str
    preheader: str | None = None
    html: str
    plain_text: str | None = None
    audience: str | None = None
    scheduled_at: datetime | None = None
