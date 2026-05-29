"""shopify sales + channels

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-29 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Sales aggregates on products
    op.add_column(
        "shopify_products",
        sa.Column("units_sold_90d", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "shopify_products",
        sa.Column(
            "unique_customers_90d", sa.Integer(), server_default="0", nullable=False
        ),
    )
    op.add_column(
        "shopify_products",
        sa.Column("last_sale_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Sync runs: also track orders
    op.add_column(
        "shopify_sync_runs",
        sa.Column("orders_count", sa.Integer(), nullable=True),
    )

    # Publications (channels)
    op.create_table(
        "shopify_publications",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=255), nullable=False),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_shopify_publications_slug", "shopify_publications", ["slug"])

    op.create_table(
        "shopify_product_publications",
        sa.Column("product_id", sa.BigInteger(), nullable=False),
        sa.Column("publication_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["product_id"], ["shopify_products.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["publication_id"], ["shopify_publications.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("product_id", "publication_id"),
    )


def downgrade() -> None:
    op.drop_table("shopify_product_publications")
    op.drop_index("ix_shopify_publications_slug", table_name="shopify_publications")
    op.drop_table("shopify_publications")
    op.drop_column("shopify_sync_runs", "orders_count")
    op.drop_column("shopify_products", "last_sale_at")
    op.drop_column("shopify_products", "unique_customers_90d")
    op.drop_column("shopify_products", "units_sold_90d")
