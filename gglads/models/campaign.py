from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from gglads.models.base import Base


class AdCampaign(Base):
    """A Google Ads campaign managed by gglads.

    Scope is one of:
      - product:    one product gets its own campaign (landing URL = product page)
      - collection: a Shopify collection gets a campaign for its themed keywords
                    (landing URL = collection page) — e.g. "Sugar-Free"
    """

    __tablename__ = "ad_campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    google_ads_campaign_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, unique=True
    )

    # Scope — exactly one of product_id / collection_id must be set
    scope_type: Mapped[str] = mapped_column(String(20), nullable=False)  # 'product' | 'collection'
    product_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("shopify_products.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    collection_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("shopify_collections.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 'draft' | 'active' | 'paused' | 'archived'
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="draft", index=True
    )

    # Budget + bidding
    daily_budget_cents: Mapped[int] = mapped_column(
        Integer, server_default="0", nullable=False
    )
    # 'maximize_conversions' | 'target_cpa' | 'maximize_clicks' | 'manual_cpc'
    bid_strategy: Mapped[str] = mapped_column(
        String(40), server_default="maximize_conversions", nullable=False
    )
    target_cpa_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)

    landing_page_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # AI settings
    ai_managed: Mapped[bool] = mapped_column(
        Boolean, server_default="false", nullable=False
    )
    ai_target_cpa_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ai_max_daily_budget_cents: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    ai_min_daily_budget_cents: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    # Wait until at least N clicks before AI acts on a keyword (statistical floor)
    ai_min_data_clicks: Mapped[int] = mapped_column(
        Integer, server_default="20", nullable=False
    )
    # JSON list of action slugs the AI is allowed to take on this campaign.
    # Examples: "pause_low_ctr", "raise_bid_high_conv", "add_negative",
    # "promote_search_term", "adjust_daily_budget".
    ai_actions_allowed_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_paused_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # Google Ads push state
    google_ads_budget_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_pushed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_push_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class AdGroup(Base):
    """One ad group within a campaign. Holds match type, keywords, and one
    Responsive Search Ad (headlines + descriptions). A typical product
    campaign has 3 ad groups: Exact, Phrase, Broad."""

    __tablename__ = "ad_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("ad_campaigns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    google_ads_ad_group_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, unique=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 'exact' | 'phrase' | 'broad'
    match_type: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), server_default="enabled", nullable=False
    )

    # Responsive Search Ad copy (one RSA per group in v1)
    headlines_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    descriptions_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Display path bits (≤15 chars each, optional)
    path1: Mapped[str | None] = mapped_column(String(15), nullable=True)
    path2: Mapped[str | None] = mapped_column(String(15), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Stale-copy detection: bumped whenever a keyword is added/removed in this
    # ad group; ad_copy_generated_at is bumped whenever copy is saved. Copy is
    # stale iff keywords_changed_at > ad_copy_generated_at.
    keywords_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ad_copy_generated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ad_copy_version: Mapped[int] = mapped_column(
        Integer, server_default="1", nullable=False
    )

    # Pending copy awaiting user approval (cron may pre-fill this when stale).
    ad_copy_pending_headlines_json: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    ad_copy_pending_descriptions_json: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    ad_copy_pending_path1: Mapped[str | None] = mapped_column(
        String(15), nullable=True
    )
    ad_copy_pending_path2: Mapped[str | None] = mapped_column(
        String(15), nullable=True
    )
    ad_copy_pending_generated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ad_copy_pending_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Google Ads RSA tracking. When the user approves new copy, the live ad
    # id moves to google_ads_prev_ad_id with a pause-at time (24h out); the
    # cron pauses it when its time comes.
    google_ads_ad_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    google_ads_prev_ad_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    google_ads_prev_ad_pause_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AdCampaignKeyword(Base):
    """One keyword (or negative) inside an ad group."""

    __tablename__ = "ad_campaign_keywords"

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("ad_campaigns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ad_group_id: Mapped[int] = mapped_column(
        ForeignKey("ad_groups.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    google_ads_keyword_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    text: Mapped[str] = mapped_column(String(255), nullable=False)
    # 'exact' | 'phrase' | 'broad' — should match the ad_group's match_type
    match_type: Mapped[str] = mapped_column(
        String(10), server_default="phrase", nullable=False
    )
    is_negative: Mapped[bool] = mapped_column(
        Boolean, server_default="false", nullable=False
    )
    cpc_bid_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), server_default="enabled", nullable=False
    )
    google_ads_resource_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
