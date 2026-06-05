"""Email campaign models — design + HTML, pushed to Shopify Email as drafts.

Reuses brand context, product data, the chat agent, the browser-agent
execution model (via ExecutionRun), and the integrations framework. Always
created as draft/scheduled — never auto-sent.
"""

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from gglads.models.base import Base


class EmailCampaign(Base):
    __tablename__ = "helena_email_campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    goal: Mapped[str | None] = mapped_column(Text, nullable=True)
    audience: Mapped[str | None] = mapped_column(Text, nullable=True)

    subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    preheader: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # JSON of A/B subject + preheader variants.
    variants_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Block layout the agent assembled (JSON list of block specs) + rendered
    # output (final inline HTML + plain-text fallback).
    layout_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    html: Mapped[str | None] = mapped_column(Text, nullable=True)
    plain_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 'draft' | 'pending_approval' | 'scheduled' | 'sent' | 'failed'
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="draft", index=True
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # External id assigned by Shopify Email (API or browser agent).
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


class EmailTemplate(Base):
    """A reusable brand-styled block definition (hero, product_grid,
    single_product, text, button, divider, footer). The renderer turns a
    layout of these into inline HTML."""

    __tablename__ = "helena_email_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Block kind: 'hero' | 'product_grid' | 'single_product' | 'text'
    # | 'button' | 'divider' | 'footer'
    kind: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # Jinja/HTML fragment with placeholders the renderer fills.
    html_fragment: Mapped[str] = mapped_column(Text, nullable=False)
    is_builtin: Mapped[bool] = mapped_column(nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class EmailAsset(Base):
    """A generated image used in an email (hero / section)."""

    __tablename__ = "helena_email_assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int | None] = mapped_column(
        ForeignKey("helena_email_campaigns.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False, server_default="hero")
    url: Mapped[str] = mapped_column(Text, nullable=False)
    alt_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
