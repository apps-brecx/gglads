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

    # Ad copy (stored as JSON for simplicity in v1; will normalize into a
    # separate ad-group/ad table once we sync to Google Ads).
    headlines_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    descriptions_json: Mapped[str | None] = mapped_column(Text, nullable=True)

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


class AdCampaignKeyword(Base):
    """One keyword (or negative) inside a campaign."""

    __tablename__ = "ad_campaign_keywords"

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("ad_campaigns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    google_ads_keyword_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    text: Mapped[str] = mapped_column(String(255), nullable=False)
    # 'exact' | 'phrase' | 'broad'
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
