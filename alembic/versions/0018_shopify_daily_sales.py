"""shopify_daily_sales table — per-day per-product per-channel rollup

Revision ID: 0018
Revises: 0017
Create Date: 2026-06-01 00:00:00.000000

product_id NULL = "all products" rollup row for that day+channel. Lets a
single query return either per-product or store-wide totals without joins.
"""
from alembic import op
import sqlalchemy as sa


revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "shopify_daily_sales",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False, index=True),
        sa.Column(
            "product_id",
            sa.BigInteger(),
            sa.ForeignKey("shopify_products.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
        # 'web' = Online Store; 'shop' = Shop app. Other channels are skipped
        # at ingest time (the user only cares about these two).
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("orders", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("units", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "revenue",
            sa.Numeric(14, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "unique_customers", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        # One row per (date, product, channel) — product_id NULL is the
        # store-wide rollup. Postgres treats NULL as distinct in UNIQUE by
        # default, which is fine because there's only one NULL-product row
        # per (date, channel) we ever insert (we never insert duplicates).
        sa.UniqueConstraint(
            "snapshot_date", "product_id", "channel",
            name="uq_shopify_daily_sales_date_product_channel",
        ),
    )
    op.create_index(
        "ix_shopify_daily_sales_date_channel",
        "shopify_daily_sales",
        ["snapshot_date", "channel"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_shopify_daily_sales_date_channel", table_name="shopify_daily_sales"
    )
    op.drop_table("shopify_daily_sales")
