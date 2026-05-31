"""product images, seo drafts, net sales, seo current values

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-29 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # New columns on shopify_products
    op.add_column(
        "shopify_products",
        sa.Column("net_sales_90d", sa.Numeric(precision=12, scale=2), nullable=True),
    )
    op.add_column(
        "shopify_products",
        sa.Column("seo_title", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "shopify_products",
        sa.Column("seo_meta_description", sa.Text(), nullable=True),
    )

    # Product images
    op.create_table(
        "shopify_product_images",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("product_id", sa.BigInteger(), nullable=False),
        sa.Column("position", sa.Integer(), server_default="0", nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("alt_text", sa.Text(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["shopify_products.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_shopify_product_images_product_id",
        "shopify_product_images",
        ["product_id"],
    )

    # SEO drafts (and image alt drafts)
    op.create_table(
        "product_seo_drafts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.BigInteger(), nullable=False),
        sa.Column("field", sa.String(length=50), nullable=False),
        sa.Column("image_id", sa.BigInteger(), nullable=True),
        sa.Column("suggested_value", sa.Text(), nullable=False),
        sa.Column(
            "status", sa.String(length=20), server_default="pending", nullable=False
        ),
        sa.Column("quality_score", sa.Integer(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by_user_id", sa.Integer(), nullable=True),
        sa.Column(
            "pushed_to_shopify_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["shopify_products.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_product_seo_drafts_product_id", "product_seo_drafts", ["product_id"]
    )
    op.create_index(
        "ix_product_seo_drafts_status", "product_seo_drafts", ["status"]
    )


def downgrade() -> None:
    op.drop_index("ix_product_seo_drafts_status", table_name="product_seo_drafts")
    op.drop_index(
        "ix_product_seo_drafts_product_id", table_name="product_seo_drafts"
    )
    op.drop_table("product_seo_drafts")
    op.drop_index(
        "ix_shopify_product_images_product_id", table_name="shopify_product_images"
    )
    op.drop_table("shopify_product_images")
    op.drop_column("shopify_products", "seo_meta_description")
    op.drop_column("shopify_products", "seo_title")
    op.drop_column("shopify_products", "net_sales_90d")
