"""shopify catalog tables

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-29 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "shopify_collections",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("handle", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column("product_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_shopify_collections_handle", "shopify_collections", ["handle"], unique=True
    )

    op.create_table(
        "shopify_products",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("handle", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("description_html", sa.Text(), nullable=True),
        sa.Column("vendor", sa.String(length=255), nullable=True),
        sa.Column("product_type", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column("price_min", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("price_max", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("first_sku", sa.String(length=255), nullable=True),
        sa.Column("total_inventory", sa.Integer(), server_default="0", nullable=False),
        sa.Column("variant_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("shopify_admin_url", sa.Text(), nullable=True),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_shopify_products_handle", "shopify_products", ["handle"], unique=True
    )
    op.create_index("ix_shopify_products_status", "shopify_products", ["status"])

    op.create_table(
        "shopify_variants",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("product_id", sa.BigInteger(), nullable=False),
        sa.Column("sku", sa.String(length=255), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=True),
        sa.Column("price", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column(
            "inventory_quantity", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("option1", sa.String(length=255), nullable=True),
        sa.Column("option2", sa.String(length=255), nullable=True),
        sa.Column("option3", sa.String(length=255), nullable=True),
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
    op.create_index("ix_shopify_variants_product_id", "shopify_variants", ["product_id"])

    op.create_table(
        "shopify_product_collections",
        sa.Column("product_id", sa.BigInteger(), nullable=False),
        sa.Column("collection_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["product_id"], ["shopify_products.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["collection_id"], ["shopify_collections.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("product_id", "collection_id"),
    )

    op.create_table(
        "shopify_sync_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=True),
        sa.Column("products_count", sa.Integer(), nullable=True),
        sa.Column("collections_count", sa.Integer(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("shopify_sync_runs")
    op.drop_table("shopify_product_collections")
    op.drop_index("ix_shopify_variants_product_id", table_name="shopify_variants")
    op.drop_table("shopify_variants")
    op.drop_index("ix_shopify_products_status", table_name="shopify_products")
    op.drop_index("ix_shopify_products_handle", table_name="shopify_products")
    op.drop_table("shopify_products")
    op.drop_index("ix_shopify_collections_handle", table_name="shopify_collections")
    op.drop_table("shopify_collections")
