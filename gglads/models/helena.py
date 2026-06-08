"""Helena module models — chat, Instagram posts, Meta ad campaigns, the
browser-agent execution log, the scheduled-task queue, and metric snapshots.

Naming note: the Meta ad campaign model is `MetaAdCampaign` to avoid colliding
with the existing Google Ads `AdCampaign` in models/campaign.py.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from gglads.models.base import Base

# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

class ChatSession(Base):
    """One Helena conversation thread, shown in the left sidebar history."""

    __tablename__ = "helena_chat_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False, server_default="New chat")
    # Optional channel scope: 'instagram' | 'meta_ads' | 'email' | 'general'
    channel: Mapped[str] = mapped_column(String(20), nullable=False, server_default="general")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class Message(Base):
    """A single turn in a ChatSession. role is user|assistant|tool. For tool
    turns, tool_name + tool_payload_json capture the skill call/result so the
    thread can be replayed and audited."""

    __tablename__ = "helena_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("helena_chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    # Optional image the user attached (pasted/uploaded) to this message.
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String(60), nullable=True)
    tool_payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


# ---------------------------------------------------------------------------
# Instagram posts
# ---------------------------------------------------------------------------

class Post(Base):
    """An Instagram post — draft, scheduled, or published via the active
    MetaExecutionProvider."""

    __tablename__ = "helena_posts"

    id: Mapped[int] = mapped_column(primary_key=True)
    caption: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    hashtags: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Publishing channel for the content calendar. One of the calendar
    # channels: blog | linkedin | x | instagram | pinterest | youtube |
    # tiktok | facebook | email. Defaults to instagram (the launch channel).
    channel: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="instagram", index=True
    )
    brand_asset_id: Mapped[int | None] = mapped_column(
        ForeignKey("brand_assets.id", ondelete="SET NULL"), nullable=True
    )
    # 'draft' | 'scheduled' | 'publishing' | 'published' | 'failed'
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="draft", index=True
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # External id / permalink returned by the provider after publish.
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    permalink: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The integration account this posts to (e.g. @syruvia_official).
    account_handle: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


# ---------------------------------------------------------------------------
# Meta ad campaigns
# ---------------------------------------------------------------------------

class MetaAdCampaign(Base):
    """A Meta (Facebook/Instagram) ad campaign managed through the active
    provider. Distinct from the Google Ads AdCampaign model."""

    __tablename__ = "helena_meta_campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    objective: Mapped[str] = mapped_column(String(40), nullable=False, server_default="traffic")
    # 'draft' | 'pending_approval' | 'active' | 'paused' | 'archived' | 'failed'
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="draft", index=True
    )
    # 'daily' | 'lifetime'
    budget_type: Mapped[str] = mapped_column(String(10), nullable=False, server_default="daily")
    budget_cents: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Free-text / JSON audience targeting description.
    audience_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Creative: a generated image + copy.
    creative_image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    creative_copy: Mapped[str | None] = mapped_column(Text, nullable=True)
    brand_asset_id: Mapped[int | None] = mapped_column(
        ForeignKey("brand_assets.id", ondelete="SET NULL"), nullable=True
    )
    # External id assigned by Meta (via browser agent or API).
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

class MetricSnapshot(Base):
    """A normalized metric row from Instagram Insights, Meta Ads, or Shopify
    Email. One row per (platform, entity, metric, date). entity_type is
    'post' | 'campaign' | 'account' | 'email'; entity_id references the
    matching Helena model (NULL for account-level totals)."""

    __tablename__ = "helena_metric_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 'instagram' | 'meta_ads' | 'email'
    platform: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    metric: Mapped[str] = mapped_column(String(40), nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric(16, 4), nullable=False, server_default="0")
    captured_for: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# Scheduled tasks + execution log (the DB-backed queue)
# ---------------------------------------------------------------------------

class ScheduledTask(Base):
    """A unit of work the cron worker drains: a one-off action (publish this
    post, push this campaign live) or a recurring job (Daily Instagram Post,
    Monday Performance Digest). Money/publish/send actions land in
    'needs_review' until a human approves.
    """

    __tablename__ = "helena_scheduled_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    # The skill / action to run, e.g. 'publish_post', 'create_ad_campaign',
    # 'update_budget', 'fetch_campaign_metrics', 'performance_digest',
    # 'create_email_draft', 'schedule_email'.
    kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    # JSON spec passed to the provider/skill.
    spec_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 'pending' | 'needs_review' | 'approved' | 'running' | 'succeeded'
    # | 'failed' | 'cancelled'
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="pending", index=True
    )
    # Whether this action spends money or publishes/sends publicly. When true
    # it must be approved (status moves needs_review -> approved) before run.
    requires_approval: Mapped[bool] = mapped_column(
        nullable=False, server_default="false"
    )

    run_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    # Recurrence: cron-like text or simple presets ('daily','weekly:mon').
    recurrence: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # When the task last executed (for the Tasks page "last ran" column).
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="3")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class ExecutionRun(Base):
    """Audit log of a single provider execution (especially browser-agent
    runs): the input spec, the steps taken, the normalized result, and any
    screenshots / links for review."""

    __tablename__ = "helena_execution_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("helena_scheduled_tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Which provider/backend ran this: 'browser' | 'api'
    backend: Mapped[str] = mapped_column(String(10), nullable=False, server_default="browser")
    # The provider method, e.g. 'createCampaign', 'publishInstagramPost'.
    action: Mapped[str] = mapped_column(String(60), nullable=False, index=True)

    input_spec_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON list of step records (what the agent did, in order).
    steps_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Normalized result JSON: {success, external_id, metrics, message}.
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON list of screenshot / confirmation URLs for audit.
    artifacts_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 'running' | 'succeeded' | 'failed' | 'needs_review'
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="running", index=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------------------
# Persistent learning memory + product image library
# ---------------------------------------------------------------------------

class MemoryItem(Base):
    """A durable fact / preference / decision Helena learned, drawn upon in
    every future chat and task. Editable on the Workspace/Memory page."""

    __tablename__ = "helena_memory_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Loose grouping: 'preference' | 'fact' | 'decision' | 'general'
    category: Mapped[str] = mapped_column(String(20), nullable=False, server_default="general")
    # 'chat' (learned automatically) | 'manual' (added on the page)
    source: Mapped[str] = mapped_column(String(10), nullable=False, server_default="chat")
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class ProductImage(Base):
    """A high-quality product (bottle) image or reference file in the library.
    Product images are labeled with flavor + variant so Helena can pull the
    correct one when generating content."""

    __tablename__ = "helena_product_images"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 'product' (a labeled bottle) | 'reference' (any other reference file)
    kind: Mapped[str] = mapped_column(String(12), nullable=False, server_default="product")
    flavor: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # 'regular' | 'sugar_free' (None for reference files)
    variant: Mapped[str | None] = mapped_column(String(12), nullable=True)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    alt_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class AdStockGuardState(Base):
    """Per-ad state for the out-of-stock ad guard. The guard matches a Meta ad
    to a Shopify product by the destination URL's product handle, pauses the ad
    when that product is out of stock (and emails an alert), and auto-resumes it
    when stock returns — unless an admin set `allow_oos` to keep it running."""

    __tablename__ = "helena_ad_stock_guard"

    # The Meta ad's external id (from the Graph API).
    ad_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ad_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    campaign_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # Shopify product handle parsed from the ad's destination URL (if any).
    product_handle: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    # True when the guard itself paused this ad (so it only auto-resumes its own).
    paused_by_guard: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # Admin override: keep running even when the product is out of stock.
    allow_oos: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # Dedupe alert emails / record context.
    last_alert_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    oos_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
