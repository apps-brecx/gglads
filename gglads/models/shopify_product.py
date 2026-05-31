from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from gglads.models.base import Base


class ShopifyCollection(Base):
    __tablename__ = "shopify_collections"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    handle: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    product_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ShopifyProduct(Base):
    __tablename__ = "shopify_products"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    handle: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    vendor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    product_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    price_min: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    price_max: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    first_sku: Mapped[str | None] = mapped_column(String(255), nullable=True)
    total_inventory: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    variant_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    shopify_admin_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    units_sold_90d: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    unique_customers_90d: Mapped[int] = mapped_column(
        Integer, server_default="0", nullable=False
    )
    last_sale_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    net_sales_90d: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)

    # SEO state pulled from Shopify
    seo_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    seo_meta_description: Mapped[str | None] = mapped_column(Text, nullable=True)

    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ShopifyVariant(Base):
    __tablename__ = "shopify_variants"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    product_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("shopify_products.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sku: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    inventory_quantity: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    option1: Mapped[str | None] = mapped_column(String(255), nullable=True)
    option2: Mapped[str | None] = mapped_column(String(255), nullable=True)
    option3: Mapped[str | None] = mapped_column(String(255), nullable=True)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ShopifyProductCollection(Base):
    __tablename__ = "shopify_product_collections"

    product_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("shopify_products.id", ondelete="CASCADE"),
        primary_key=True,
    )
    collection_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("shopify_collections.id", ondelete="CASCADE"),
        primary_key=True,
    )


class ShopifyPublication(Base):
    __tablename__ = "shopify_publications"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ShopifyProductPublication(Base):
    __tablename__ = "shopify_product_publications"

    product_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("shopify_products.id", ondelete="CASCADE"),
        primary_key=True,
    )
    publication_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("shopify_publications.id", ondelete="CASCADE"),
        primary_key=True,
    )


class ShopifyProductImage(Base):
    __tablename__ = "shopify_product_images"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    product_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("shopify_products.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    position: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    alt_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ProductSeoDraft(Base):
    """AI-generated suggestion for a product SEO field or image alt.

    field values:
      - 'seo_title', 'meta_description', 'description', 'bullets', 'image_alt'

    For image_alt drafts, image_id is set.
    """

    __tablename__ = "product_seo_drafts"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("shopify_products.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    field: Mapped[str] = mapped_column(String(50), nullable=False)
    image_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    suggested_value: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="pending", index=True
    )
    # verdict: 'improve' = AI wants to change; 'keep' = AI says current is already strong
    verdict: Mapped[str | None] = mapped_column(String(20), nullable=True)
    quality_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    pushed_to_shopify_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ShopifyInventorySnapshot(Base):
    __tablename__ = "shopify_inventory_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "product_id", "snapshot_date", name="uq_inventory_snapshot_product_date"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("shopify_products.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    inventory: Mapped[int] = mapped_column(Integer, nullable=False)
    is_in_stock: Mapped[bool] = mapped_column(Boolean, nullable=False)


class ShopifySyncRun(Base):
    __tablename__ = "shopify_sync_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    products_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    collections_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    orders_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
