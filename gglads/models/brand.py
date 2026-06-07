"""Structured brand knowledge base + brand assets.

Before Helena, brand context lived informally in global ProductChatMessage
rows (product_id IS NULL) and the (stub) Training page. Helena needs a
structured, editable record — visual style, mood, audience, content themes,
tone — plus first-class brand assets (logo, colors, saved generated images)
that image + copy generation can inject.

There is a single active Brand row per store (id=1 by convention); the model
supports more so a future multi-brand setup is a non-breaking change.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from gglads.models.base import Base


class Brand(Base):
    __tablename__ = "brands"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Free-text brand knowledge, all optional and editable in the UI. Injected
    # into image prompts and copy generation.
    tone: Mapped[str | None] = mapped_column(Text, nullable=True)
    visual_style: Mapped[str | None] = mapped_column(Text, nullable=True)
    mood: Mapped[str | None] = mapped_column(Text, nullable=True)
    audience: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_themes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON list of brand hex colors, e.g. ["#FF5CA8", "#1A1A1A"].
    palette_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Free-form extra guidance (banned words, claims to avoid, etc.).
    guidelines: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class BrandAsset(Base):
    """A reusable visual asset: logo, an uploaded reference, or a chosen
    AI-generated image saved for reuse in posts / ads / emails."""

    __tablename__ = "brand_assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(
        ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 'logo' | 'reference' | 'generated' | 'product'
    kind: Mapped[str] = mapped_column(String(20), nullable=False, server_default="generated")
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    # When kind='generated', the prompt/concept that produced it (for re-gen).
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Optional link back to the originating Shopify product. Shopify product
    # IDs are BIGINT, so this must be BigInteger (a plain Integer overflows).
    product_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class BrandDocument(Base):
    """A document the agent treats as persistent brand memory — pasted text or
    a linked file. Titles + excerpts are injected into the agent's brand
    context so it can draw on them across conversations."""

    __tablename__ = "brand_documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(
        ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
