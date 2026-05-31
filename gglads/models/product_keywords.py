from datetime import datetime

from sqlalchemy import (
    Boolean,
    BigInteger,
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
