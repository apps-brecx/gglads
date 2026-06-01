from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    BigInteger,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from gglads.models.base import Base


class ProductKeyword(Base):
    __tablename__ = "product_keywords"
    __table_args__ = (
        UniqueConstraint("product_id", "keyword", name="uq_product_keyword"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("shopify_products.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    keyword: Mapped[str] = mapped_column(String(255), nullable=False)

    # Claude-assigned classification
    intent: Mapped[str | None] = mapped_column(String(20), nullable=True)
    funnel: Mapped[str | None] = mapped_column(String(20), nullable=True)
    match_type: Mapped[str | None] = mapped_column(String(10), nullable=True)
    relevance_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(30), nullable=False, default="ai")

    # Keyword Planner enrichment (Google Ads)
    avg_monthly_searches: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    competition: Mapped[str | None] = mapped_column(String(10), nullable=True)
    low_bid_micros: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    high_bid_micros: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Search Console enrichment (organic)
    sc_clicks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sc_impressions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sc_ctr: Mapped[float | None] = mapped_column(Float, nullable=True)
    sc_position: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Triage state
    bucket: Mapped[str] = mapped_column(
        String(15), nullable=False, server_default="unsorted", index=True
    )
    # JSON-encoded list of SEO field slugs where this keyword should be included
    # on the next AI generation. e.g. ["title", "meta_description", "description"]
    seo_targets: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class KeywordResearchRun(Base):
    __tablename__ = "keyword_research_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("shopify_products.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    sources_used: Mapped[str | None] = mapped_column(String(255), nullable=True)
    keywords_added: Mapped[int | None] = mapped_column(Integer, nullable=True)
    keywords_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON dict: {source_slug: error_message or null}
    source_errors: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class ProductKeywordHistory(Base):
    """Per-day per-product per-keyword snapshot of Search Console metrics.
    Filled by cron/keyword_history_sweep — one row per (date, product, keyword)
    for every query SC reports as having landed on the product URL that day."""

    __tablename__ = "product_keyword_history"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_date", "product_id", "keyword",
            name="uq_pkh_date_product_keyword",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    product_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("shopify_products.id", ondelete="CASCADE"),
        nullable=False,
    )
    keyword: Mapped[str] = mapped_column(String(255), nullable=False)
    sc_position: Mapped[float | None] = mapped_column(Float, nullable=True)
    sc_clicks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sc_impressions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sc_ctr: Mapped[float | None] = mapped_column(Float, nullable=True)


class CollectionSuggestion(Base):
    """AI-generated proposal for a new Shopify collection. Anchored on
    organic-search clusters that the store ranks for but doesn't have a
    collection page for. Status: pending → user dismisses or marks created."""

    __tablename__ = "collection_suggestions"
    __table_args__ = (
        UniqueConstraint(
            "handle", "status", name="uq_collection_suggestion_handle_status"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    handle: Mapped[str] = mapped_column(String(255), nullable=False)
    theme_keywords_json: Mapped[str] = mapped_column(Text, nullable=False)
    seo_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    seo_meta_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    opportunity_score: Mapped[int] = mapped_column(
        Integer, server_default="50", nullable=False
    )
    # 'pending' | 'dismissed' | 'created'
    status: Mapped[str] = mapped_column(
        String(20), server_default="pending", nullable=False, index=True
    )
    created_collection_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
